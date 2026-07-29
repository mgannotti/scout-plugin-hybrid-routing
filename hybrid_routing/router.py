"""Routing engine — turns a classification into an executable decision.

Selection order mirrors the Hermes original (sensitivity, then role, then
difficulty tier) but every candidate passes through an egress filter first.
A task classified `restricted` can only ever be offered on-device models —
including in its fallback chain.

When nothing satisfies the ceiling the router returns a **blocked** decision
rather than degrading to a less-trusted model. The Hermes original does the
opposite: with no local model configured it sends sensitive content to the
balanced cloud tier, which is the precise outcome the feature exists to
prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .classify import (
    CONFIDENTIAL,
    DIFFICULTY_TO_TIER,
    NORMAL,
    PROVENANCE_MODES,
    RESTRICTED,
    SENSITIVITY_LEVELS,
    Classifier,
    normalize_provenance_mode,
    normalize_sensitivity,
)
from .egress import (
    CHANNEL_SCOUT_TASK,
    EGRESS_CLASSES,
    ModelRef,
    ModelRefError,
    normalize_egress,
    parse_model_ref,
    permits,
    resolve_backend_egress,
    resolve_egress,
)

TIERS = ("fast", "balanced", "strong")

# Recognized config keys. Anything else is a typo that silently does nothing,
# which for a security control is a failure mode worth reporting.
KNOWN_TOP_LEVEL_KEYS = frozenset(
    {
        "backends",
        "tiers",
        "roles",
        "sensitivity",
        "difficulty",
        "policy",
        "delegation",
        "provenance",
    }
)
KNOWN_SUBKEYS = {
    "sensitivity": frozenset(
        {
            "confidential_model",
            "restricted_model",
            "restricted_patterns",
            "confidential_patterns",
        }
    ),
    "policy": frozenset({"max_egress"}),
    "delegation": frozenset({"primary_model", "skip_for_tier", "skip_if_same_as_primary"}),
    "provenance": frozenset({"mode", "default_level", "sources"}),
}

# Outcomes
ROUTE_OK = "ok"
ROUTE_BLOCKED = "blocked"
ROUTE_UNCONFIGURED = "unconfigured"


@dataclass
class Candidate:
    """A model that satisfies the egress ceiling, with why it was offered."""

    ref: ModelRef
    origin: str
    egress: str
    effort: str | None = None

    def to_dict(self) -> dict:
        data = self.ref.to_dict()
        data["origin"] = self.origin
        # The reconciled class, which may be less trusted than the scheme claims.
        data["egress"] = self.egress
        if self.effort:
            data["reasoning_effort"] = self.effort
        return data


@dataclass
class RoutingDecision:
    """The routing outcome, including how to execute it."""

    status: str
    model: str
    egress: str
    channel: str
    backend: str
    model_id: str
    tier: str
    role: str
    difficulty: str
    sensitivity: str
    ceiling: str
    should_delegate: bool
    reason: str
    reasoning_effort: str | None = None
    signals: list[str] = field(default_factory=list)
    fallbacks: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    execution: dict = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.status != ROUTE_OK

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "model": self.model,
            "egress": self.egress,
            "channel": self.channel,
            "backend": self.backend,
            "model_id": self.model_id,
            "tier": self.tier,
            "role": self.role,
            "difficulty": self.difficulty,
            "sensitivity": self.sensitivity,
            "egress_ceiling": self.ceiling,
            "should_delegate": self.should_delegate,
            "reasoning_effort": self.reasoning_effort,
            "reason": self.reason,
            "signals": list(self.signals),
            "fallback_chain": list(self.fallbacks),
            "rejected_for_egress": list(self.rejected),
            "execution": dict(self.execution),
        }


class HybridRouter:
    """Classifies a task and selects a model that respects its egress ceiling."""

    def __init__(self, config: dict):
        self.config = config or {}
        self.classifier = Classifier(self.config)

    # ── Config accessors ───────────────────────────────────────────────
    def _tier_entry(self, tier: str) -> dict:
        return (self.config.get("tiers", {}) or {}).get(tier, {}) or {}

    def _tier_model(self, tier: str) -> str:
        return str(self._tier_entry(tier).get("model", "") or "").strip()

    def _tier_effort(self, tier: str) -> str | None:
        effort = self._tier_entry(tier).get("reasoning_effort")
        return str(effort).strip() if effort else None

    def _role_entry(self, role: str) -> dict:
        return (self.config.get("roles", {}) or {}).get(role, {}) or {}

    def _role_model(self, role: str) -> str:
        return str(self._role_entry(role).get("model", "") or "").strip()

    def _sensitive_model(self, level: str) -> str:
        sens = self.config.get("sensitivity", {}) or {}
        key = "restricted_model" if level == RESTRICTED else "confidential_model"
        return str(sens.get(key, "") or "").strip()

    def _primary_model(self) -> str:
        deleg = self.config.get("delegation", {}) or {}
        return str(deleg.get("primary_model", "") or "").strip()

    def configured_models(self) -> list[tuple[str, str]]:
        """Every non-blank configured model as (origin, ref)."""
        found: list[tuple[str, str]] = []
        for tier in TIERS:
            model = self._tier_model(tier)
            if model:
                found.append((f"tier:{tier}", model))
        for role in sorted((self.config.get("roles", {}) or {})):
            model = self._role_model(role)
            if model:
                found.append((f"role:{role}", model))
        for level in (RESTRICTED, CONFIDENTIAL):
            model = self._sensitive_model(level)
            if model:
                found.append((f"sensitivity:{level}", model))
        return found

    def is_configured(self) -> bool:
        return bool(self.configured_models())

    def validate(self) -> list[str]:
        """Return a list of config problems. Empty means the config is sound."""
        problems: list[str] = []
        backends = self.config.get("backends", {}) or {}

        # A mistyped section name silently removes whatever it configured. The
        # `sensitivty:` typo, for instance, deletes every detection pattern
        # while the file still looks correct at a glance.
        for key in sorted(self.config):
            if key not in KNOWN_TOP_LEVEL_KEYS:
                problems.append(
                    f"unknown top-level config key {key!r} is being ignored "
                    f"(expected one of: {', '.join(sorted(KNOWN_TOP_LEVEL_KEYS))}). "
                    f"If this is a typo, the settings under it are not in effect."
                )
        for section, allowed in KNOWN_SUBKEYS.items():
            for key in sorted((self.config.get(section) or {})):
                if key not in allowed:
                    problems.append(
                        f"unknown key {section}.{key!r} is being ignored "
                        f"(expected one of: {', '.join(sorted(allowed))})."
                    )

        # A protection rule that failed to compile is worse than no rule.
        problems.extend(self.classifier.pattern_errors)

        # Provenance settings that cannot be read fail closed to `floor`, but
        # the user should know their setting is not the one in effect.
        prov = self.config.get("provenance", {}) or {}
        raw_mode = prov.get("mode")
        if raw_mode not in (None, "") and normalize_provenance_mode(raw_mode) is None:
            problems.append(
                f"provenance.mode is {raw_mode!r}, which is not one of "
                f"{', '.join(PROVENANCE_MODES)}. Failing closed to 'floor'."
            )
        raw_level = prov.get("default_level")
        if raw_level not in (None, "") and normalize_sensitivity(raw_level) is None:
            problems.append(
                f"provenance.default_level is {raw_level!r}, which is not one of "
                f"{', '.join(SENSITIVITY_LEVELS)}. Failing closed to 'confidential'."
            )
        for name, value in (prov.get("sources") or {}).items():
            if normalize_sensitivity(value) is None:
                problems.append(
                    f"provenance.sources[{name!r}] is {value!r}, which is not one of "
                    f"{', '.join(SENSITIVITY_LEVELS)}. Failing closed."
                )

        # A policy cap that cannot be understood must not silently do nothing.
        raw_cap = (self.config.get("policy", {}) or {}).get("max_egress")
        if raw_cap not in (None, "") and normalize_egress(raw_cap) is None:
            problems.append(
                f"policy.max_egress is {raw_cap!r}, which is not one of "
                f"{', '.join(EGRESS_CLASSES)}. It is being treated as "
                f"'on-device' (fail-closed) until corrected."
            )

        for name, entry in backends.items():
            if not str((entry or {}).get("base_url", "") or "").strip():
                problems.append(f"backend {name!r} has no base_url.")

        for origin, ref in self.configured_models():
            try:
                parsed = parse_model_ref(ref)
            except ModelRefError as exc:
                problems.append(f"{origin}: {exc}")
                continue
            # Surface every scheme/backend disagreement, not just the ones that
            # happen to matter for the sensitivity slots.
            for conflict in resolve_egress(parsed, backends).conflicts:
                problems.append(f"{origin}: {conflict}")

        primary = self._primary_model()
        if primary:
            try:
                parse_model_ref(primary)
            except ModelRefError as exc:
                problems.append(f"delegation.primary_model: {exc}")

        # A sensitivity model outside its required boundary defeats the purpose.
        # Checked against the RESOLVED class, not the claimed one.
        for level, required in ((RESTRICTED, "on-device"), (CONFIDENTIAL, "org-tenant")):
            ref = self._sensitive_model(level)
            if not ref:
                continue
            try:
                parsed = parse_model_ref(ref)
            except ModelRefError:
                continue
            resolved = resolve_egress(parsed, backends)
            if not permits(required, resolved.egress):
                problems.append(
                    f"sensitivity.{level}_model {ref!r} resolves to egress "
                    f"{resolved.egress!r}, which is outside the required "
                    f"{required!r} boundary for {level} content"
                )
        return problems

    # ── Candidate assembly ─────────────────────────────────────────────
    def _ordered_candidates(self, classification) -> list[tuple[str, str, str | None]]:
        """Preference-ordered (origin, ref, effort) before egress filtering."""
        ordered: list[tuple[str, str, str | None]] = []
        seen: set[str] = set()

        def add(origin: str, ref: str, effort: str | None = None) -> None:
            if ref and ref not in seen:
                seen.add(ref)
                ordered.append((origin, ref, effort))

        level = classification.sensitivity
        tier = classification.tier

        if level != NORMAL:
            add(f"sensitivity:{level}", self._sensitive_model(level))
            if level == RESTRICTED:
                add("sensitivity:confidential", self._sensitive_model(CONFIDENTIAL))

        if classification.role != "general":
            add(
                f"role:{classification.role}",
                self._role_model(classification.role),
                self._role_entry(classification.role).get("reasoning_effort"),
            )

        add(f"tier:{tier}", self._tier_model(tier), self._tier_effort(tier))

        # Remaining tiers, strongest-adjacent first, as graceful fallback.
        for other in ("strong", "balanced", "fast"):
            if other != tier:
                add(f"tier:{other}", self._tier_model(other), self._tier_effort(other))

        # Any role model at all is better than nothing for a restricted task.
        for role in sorted((self.config.get("roles", {}) or {})):
            add(f"role:{role}", self._role_model(role))

        return ordered

    # ── Main entry point ───────────────────────────────────────────────
    def route(
        self, text: str, label: str | None = None, source: str | None = None
    ) -> RoutingDecision:
        classification = self.classifier.classify(text, label=label, source=source)

        if not self.is_configured():
            return RoutingDecision(
                status=ROUTE_UNCONFIGURED,
                model="",
                egress="",
                channel="",
                backend="",
                model_id="",
                tier=classification.tier,
                role=classification.role,
                difficulty=classification.difficulty,
                sensitivity=classification.sensitivity,
                ceiling=classification.ceiling,
                should_delegate=False,
                reason=(
                    "No models configured. Copy data/routing_config.yaml to "
                    "~/.scout/hybrid_routing/routing_config.yaml and fill in your "
                    "model references, or run `python -m hybrid_routing init`."
                ),
                signals=classification.signals,
            )

        ceiling = classification.ceiling
        accepted: list[Candidate] = []
        rejected: list[dict] = []
        backends = self.config.get("backends", {}) or {}

        for origin, ref, effort in self._ordered_candidates(classification):
            try:
                parsed = parse_model_ref(ref)
            except ModelRefError as exc:
                rejected.append({"ref": ref, "origin": origin, "why": str(exc)})
                continue

            # The scheme is a claim; reconcile it against the backend's real
            # destination before trusting it for a routing decision.
            resolved = resolve_egress(parsed, backends)
            if permits(ceiling, resolved.egress):
                accepted.append(
                    Candidate(
                        ref=parsed, origin=origin, egress=resolved.egress, effort=effort
                    )
                )
            else:
                why = (
                    f"egress {resolved.egress!r} exceeds ceiling {ceiling!r} "
                    f"for {classification.sensitivity} content"
                )
                if resolved.conflicts:
                    why += f"; {resolved.conflicts[0]}"
                rejected.append(
                    {
                        "ref": ref,
                        "origin": origin,
                        "egress": resolved.egress,
                        "claimed_egress": parsed.egress,
                        "why": why,
                    }
                )

        if not accepted:
            return self._blocked(classification, rejected)

        chosen = accepted[0]
        effort = chosen.effort or self._tier_effort(classification.tier)
        reason = self._explain(classification, chosen, rejected)
        should_delegate = self._should_delegate(classification, chosen)

        return RoutingDecision(
            status=ROUTE_OK,
            model=chosen.ref.raw,
            egress=chosen.egress,
            channel=chosen.ref.channel,
            backend=chosen.ref.backend,
            model_id=chosen.ref.model_id,
            tier=classification.tier,
            role=classification.role,
            difficulty=classification.difficulty,
            sensitivity=classification.sensitivity,
            ceiling=ceiling,
            should_delegate=should_delegate,
            reason=reason,
            reasoning_effort=effort,
            signals=classification.signals,
            fallbacks=[c.to_dict() for c in accepted[1:]],
            rejected=rejected,
            execution=self._execution(chosen, effort, should_delegate),
        )

    # ── Helpers ────────────────────────────────────────────────────────
    def _blocked(self, classification, rejected: list[dict]) -> RoutingDecision:
        need = classification.ceiling
        blocked_by_egress = [r for r in rejected if r.get("egress")]
        if blocked_by_egress:
            detail = (
                f"{len(blocked_by_egress)} configured model(s) were rejected because "
                f"their egress class exceeds the {need!r} boundary"
            )
        else:
            detail = "no configured model could be parsed"
        return RoutingDecision(
            status=ROUTE_BLOCKED,
            model="",
            egress="",
            channel="",
            backend="",
            model_id="",
            tier=classification.tier,
            role=classification.role,
            difficulty=classification.difficulty,
            sensitivity=classification.sensitivity,
            ceiling=need,
            should_delegate=False,
            reason=(
                f"BLOCKED: content classified {classification.sensitivity!r} requires a "
                f"model at or inside the {need!r} boundary, and none is configured. "
                f"{detail}. Configure sensitivity.{classification.sensitivity}_model with "
                f"a local/ or org/ model, or handle this task without a model. "
                f"Routing to an available cloud model would send this content outside "
                f"the required boundary."
            ),
            signals=classification.signals,
            rejected=rejected,
        )

    def _should_delegate(self, classification, chosen: Candidate) -> bool:
        deleg = self.config.get("delegation", {}) or {}
        primary = self._primary_model()

        # Anything that is not a Scout cloud model cannot run in the primary
        # session at all — it must be executed out of band.
        if chosen.ref.channel != CHANNEL_SCOUT_TASK:
            return True
        if classification.sensitivity != NORMAL:
            return True
        if deleg.get("skip_if_same_as_primary", True) and chosen.ref.raw == primary:
            return False
        if classification.tier in (deleg.get("skip_for_tier", []) or []):
            return False
        return True

    def _explain(self, classification, chosen: Candidate, rejected: list[dict]) -> str:
        origin = chosen.origin
        if origin.startswith("sensitivity:"):
            head = (
                f"Content classified {classification.sensitivity!r} -> routed to "
                f"{chosen.ref.raw!r} ({chosen.egress})"
            )
        elif origin.startswith("role:"):
            head = (
                f"Role {classification.role!r} -> routed to role model "
                f"{chosen.ref.raw!r} ({chosen.egress})"
            )
        else:
            head = (
                f"Difficulty {classification.difficulty!r} (tier {classification.tier!r}) "
                f"-> routed to {chosen.ref.raw!r} ({chosen.egress})"
            )
        if chosen.egress != chosen.ref.egress:
            head += (
                f"; NOTE: reference claims {chosen.ref.egress!r} but its backend "
                f"resolves to {chosen.egress!r}"
            )
        if classification.sensitivity != NORMAL:
            head += f"; egress ceiling {classification.ceiling!r} enforced"
        blocked = [r for r in rejected if r.get("egress")]
        if blocked:
            head += f"; {len(blocked)} cloud/less-trusted model(s) excluded"
        return head

    def _execution(self, chosen: Candidate, effort: str | None, delegate: bool) -> dict:
        """Concrete instructions for actually running the selected model."""
        if chosen.ref.channel == CHANNEL_SCOUT_TASK:
            call = {
                "tool": "task",
                "agent_type": "general-purpose",
                "model": chosen.ref.model_id,
            }
            if effort:
                call["reasoning_effort"] = effort
            return {
                "mode": "scout-task" if delegate else "inline",
                "call": call,
                "note": (
                    "Delegate via Scout's task tool so the primary session model "
                    "stays fixed and prompt caching is preserved."
                    if delegate
                    else "Selected model matches the primary session model; handle inline."
                ),
            }
        return {
            "mode": "openai-http",
            "backend": chosen.ref.backend,
            "model": chosen.ref.model_id,
            "egress": chosen.egress,
            "cli": (
                f"python -m hybrid_routing infer --backend {chosen.ref.backend} "
                f"--model {chosen.ref.model_id} --prompt-file <file>"
            ),
            "note": (
                f"Runs on the {chosen.ref.backend!r} backend ({chosen.egress}). "
                "Scout's task tool cannot host this model; call the backend over "
                "its OpenAI-compatible endpoint."
            ),
        }

    # ── Execution authorization ────────────────────────────────────────
    def authorize_inference(
        self,
        backend_name: str,
        text: str,
        label: str | None = None,
        source: str | None = None,
    ) -> dict:
        """Decide whether `text` may be sent to `backend_name`.

        Routing decisions are only advice until something enforces them at the
        point of transmission. Without this, `route_infer` would accept any
        backend for any content — a caller could hand a restricted prompt
        straight to a cloud endpoint and never consult the router at all,
        which would make the whole egress model decorative.
        """
        classification = self.classifier.classify(text, label=label, source=source)
        resolved = resolve_backend_egress(backend_name, self.config.get("backends", {}) or {})
        allowed = permits(classification.ceiling, resolved.egress)
        result = {
            "allowed": allowed,
            "backend": backend_name,
            "backend_egress": resolved.egress,
            "sensitivity": classification.sensitivity,
            "egress_ceiling": classification.ceiling,
            "signals": classification.signals,
            "conflicts": resolved.conflicts,
        }
        if not allowed:
            result["reason"] = (
                f"REFUSED: this prompt classifies as {classification.sensitivity!r}, "
                f"which may only reach a {classification.ceiling!r} destination, but "
                f"backend {backend_name!r} resolves to {resolved.egress!r}. "
                f"Route the task with route_classify and use the backend it selects."
            )
        return result

    # ── Introspection ──────────────────────────────────────────────────
    def status(self) -> dict:
        entries = []
        backends = self.config.get("backends", {}) or {}
        for origin, ref in self.configured_models():
            try:
                parsed = parse_model_ref(ref)
            except ModelRefError as exc:
                entries.append({"origin": origin, "ref": ref, "error": str(exc)})
                continue
            resolved = resolve_egress(parsed, backends)
            entry = {"origin": origin, **parsed.to_dict()}
            entry["claimed_egress"] = parsed.egress
            entry["egress"] = resolved.egress
            if resolved.conflicts:
                entry["conflicts"] = resolved.conflicts
            entries.append(entry)
        return {
            "configured": self.is_configured(),
            "problems": self.validate(),
            "models": entries,
            "policy": self.config.get("policy", {}) or {},
            "delegation": self.config.get("delegation", {}) or {},
            "backends": sorted(backends.keys()),
        }
