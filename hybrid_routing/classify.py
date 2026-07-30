"""Task classification — sensitivity, role, difficulty.

Three signals, computed independently from the task text plus optional
out-of-band context (a Microsoft Information Protection label, and the M365
surface the content came from).

Sensitivity is graded rather than binary. The Hermes original has
normal/sensitive, which cannot express "this is internal business content, a
tenant-hosted model is fine" — a distinction that matters constantly in
Microsoft 365 work. Here:

    normal        no markers          -> cloud permitted
    confidential  internal business   -> tenant boundary
    restricted    secrets / PII / PHI -> on-device only
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .egress import CLOUD_PUBLIC, ON_DEVICE, ORG_TENANT, normalize_egress, tighter
from .validators import VALIDATORS

# ── Sensitivity levels ─────────────────────────────────────────────────
NORMAL = "normal"
CONFIDENTIAL = "confidential"
RESTRICTED = "restricted"

SENSITIVITY_LEVELS = (NORMAL, CONFIDENTIAL, RESTRICTED)

_SENSITIVITY_RANK = {NORMAL: 0, CONFIDENTIAL: 1, RESTRICTED: 2}

# Sensitivity level -> the least-trusted boundary the content may cross.
SENSITIVITY_CEILING = {
    NORMAL: CLOUD_PUBLIC,
    CONFIDENTIAL: ORG_TENANT,
    RESTRICTED: ON_DEVICE,
}

# ── Difficulty ─────────────────────────────────────────────────────────
SIMPLE = "simple"
STANDARD = "standard"
HARD = "hard"

DIFFICULTY_TO_TIER = {SIMPLE: "fast", STANDARD: "balanced", HARD: "strong"}

# ── M365 provenance ────────────────────────────────────────────────────
# Content read out of the user's tenant is business content by default, even
# when it carries no explicit label. How much that matters is deployment-
# specific, so it is configurable: a regulated environment wants every M365
# read floored at confidential, while an individual mostly wants the label and
# pattern signals and would find a blanket floor unusable.
M365_SOURCES = frozenset(
    {"email", "teams", "calendar", "sharepoint", "onedrive", "planner", "todo", "transcript"}
)
PUBLIC_SOURCES = frozenset({"web", "user", "local", "repo", ""})

PROVENANCE_OFF = "off"  # ignore provenance entirely
PROVENANCE_ADVISORY = "advisory"  # record it, but do not raise the level
PROVENANCE_FLOOR = "floor"  # raise to the configured level (default)
PROVENANCE_MODES = (PROVENANCE_OFF, PROVENANCE_ADVISORY, PROVENANCE_FLOOR)

# ── MIP label normalization ────────────────────────────────────────────
_LABEL_RESTRICTED = ("highly confidential", "restricted", "secret", "top secret")
_LABEL_CONFIDENTIAL = ("confidential", "internal", "internal only", "proprietary", "sensitive")
_LABEL_PUBLIC = ("public", "general", "non-business", "unrestricted")


def normalize_label(label: str | None) -> str:
    """Map an MIP sensitivity label to a sensitivity level.

    Checked most-restrictive first so "Highly Confidential" is not swallowed by
    the "confidential" substring.
    """
    if not label:
        return NORMAL
    text = label.strip().lower()
    if any(marker in text for marker in _LABEL_RESTRICTED):
        return RESTRICTED
    if any(marker in text for marker in _LABEL_CONFIDENTIAL):
        return CONFIDENTIAL
    if any(marker in text for marker in _LABEL_PUBLIC):
        return NORMAL
    return NORMAL


def normalize_sensitivity(value: str | None) -> str | None:
    """Normalize a sensitivity level name; None if unrecognized."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return text if text in SENSITIVITY_LEVELS else None


def normalize_provenance_mode(value: str | None) -> str | None:
    """Normalize a provenance mode name; None if unrecognized."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return text if text in PROVENANCE_MODES else None


def max_sensitivity(*levels: str) -> str:
    """Return the most restrictive of the supplied sensitivity levels."""
    best = NORMAL
    for level in levels:
        if _SENSITIVITY_RANK.get(level, 0) > _SENSITIVITY_RANK.get(best, 0):
            best = level
    return best


@dataclass
class Classification:
    """The three signals plus the egress ceiling they imply."""

    sensitivity: str
    role: str
    difficulty: str
    tier: str
    ceiling: str
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sensitivity": self.sensitivity,
            "role": self.role,
            "difficulty": self.difficulty,
            "tier": self.tier,
            "egress_ceiling": self.ceiling,
            "signals": list(self.signals),
        }


class _ValidatedPattern:
    """A compiled regex paired with a check on the matched value.

    Duck-types the slice of `re.Pattern` the classifier uses, so call sites
    are unchanged. `search` walks every regex match and returns the first one
    the validator accepts — a rejected match must not mask a real one later
    in the text, e.g. an order number sitting in front of a card number.
    """

    __slots__ = ("regex", "_validator", "validator_name")

    def __init__(self, regex: re.Pattern, validator, validator_name: str):
        self.regex = regex
        self._validator = validator
        self.validator_name = validator_name

    def search(self, text: str):
        for match in self.regex.finditer(text or ""):
            if self._validator(match.group(0)):
                return match
        return None

    @property
    def pattern(self) -> str:
        return self.regex.pattern

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"_ValidatedPattern({self.regex.pattern!r}, validator={self.validator_name!r})"


class Classifier:
    """Compiles config-driven patterns once and classifies task text."""

    def __init__(self, config: dict):
        self._cfg = config or {}
        sens_cfg = self._cfg.get("sensitivity", {}) or {}

        # Pattern compilation failures are collected, never swallowed. A
        # protection rule that silently vanishes because of a typo is worse
        # than no rule at all: the config still claims the content is covered.
        self.pattern_errors: list[str] = []

        self._restricted = self._compile_all(
            sens_cfg.get("restricted_patterns", []), "sensitivity.restricted_patterns"
        )
        self._confidential = self._compile_all(
            sens_cfg.get("confidential_patterns", []), "sensitivity.confidential_patterns"
        )

        diff_cfg = self._cfg.get("difficulty", {}) or {}
        self._hard = self._compile_all(diff_cfg.get("hard_cues", []), "difficulty.hard_cues")
        self._simple = self._compile_all(diff_cfg.get("simple_cues", []), "difficulty.simple_cues")
        self._diff_cfg = diff_cfg

        self._role_cues: dict[str, list[re.Pattern]] = {}
        for role_name, role_cfg in (self._cfg.get("roles", {}) or {}).items():
            cues = (role_cfg or {}).get("cues", []) or []
            if cues:
                self._role_cues[role_name] = [_compile_cue(cue) for cue in cues]

    def _compile_all(self, patterns, where: str) -> list:
        """Compile a pattern list.

        An entry is either a plain regex string, or a mapping declaring a
        value validator:

            - "\\b\\d{3}-\\d{2}-\\d{4}\\b"
            - pattern: "\\b(?:\\d[ -]?){12,18}\\d\\b"
              validator: luhn

        Plain strings compile to a bare `re.Pattern` exactly as before; only
        entries that ask for a validator are wrapped. Both forms expose
        `.search(text)`, so callers do not care which they were given.
        """
        compiled = []
        for index, entry in enumerate(patterns or []):
            pattern, validator_name = entry, None
            if isinstance(entry, dict):
                pattern = entry.get("pattern")
                validator_name = entry.get("validator")
                unknown = set(entry) - {"pattern", "validator"}
                if unknown:
                    self.pattern_errors.append(
                        f"{where}[{index}] has unknown key(s): {', '.join(sorted(unknown))}"
                    )

            try:
                rx = re.compile(pattern, re.IGNORECASE)
            except (re.error, TypeError) as exc:
                self.pattern_errors.append(f"{where}[{index}] is not a valid regex: {exc}")
                continue

            if validator_name is None:
                compiled.append(rx)
                continue

            validator = VALIDATORS.get(str(validator_name).strip().lower())
            if validator is None:
                # Fail closed. Dropping the rule would leave the config
                # claiming a protection that is not running; keeping it
                # unvalidated silently widens it. Report and keep the regex.
                self.pattern_errors.append(
                    f"{where}[{index}] names unknown validator "
                    f"{validator_name!r}; known validators: "
                    f"{', '.join(sorted(VALIDATORS))}"
                )
                compiled.append(rx)
                continue

            compiled.append(_ValidatedPattern(rx, validator, str(validator_name)))
        return compiled

    # ── Provenance ─────────────────────────────────────────────────────
    def _provenance_cfg(self) -> dict:
        return self._cfg.get("provenance", {}) or {}

    def provenance_mode(self) -> str:
        """Effective mode. An unreadable value fails closed to `floor`."""
        raw = self._provenance_cfg().get("mode")
        if raw in (None, ""):
            return PROVENANCE_FLOOR
        return normalize_provenance_mode(raw) or PROVENANCE_FLOOR

    def _provenance_default_level(self) -> str:
        raw = self._provenance_cfg().get("default_level")
        if raw in (None, ""):
            return CONFIDENTIAL
        return normalize_sensitivity(raw) or CONFIDENTIAL

    def _provenance_level(self, src: str) -> tuple[str | None, str]:
        """Return (level to apply or None, explanation) for a source."""
        cfg = self._provenance_cfg()
        overrides = {
            str(k).strip().lower(): v for k, v in (cfg.get("sources") or {}).items()
        }

        # A per-source entry wins over the mode, in either direction: it can
        # exempt a surface the mode would floor, or floor one the mode ignores.
        if src in overrides:
            raw = overrides[src]
            level = normalize_sensitivity(raw)
            if level is None:
                level = self._provenance_default_level()
                return level, (
                    f"provenance.sources[{src!r}] is {raw!r}, which is not a "
                    f"sensitivity level; failing closed to {level}"
                )
            if level == NORMAL:
                return None, f"provenance {src!r} configured as normal; no floor applied"
            return level, f"provenance override {src!r} -> {level}"

        if src not in M365_SOURCES:
            return None, ""

        mode = self.provenance_mode()
        if mode == PROVENANCE_OFF:
            return None, ""
        if mode == PROVENANCE_ADVISORY:
            return None, (
                f"M365 provenance ({src}) noted; provenance.mode=advisory, so the "
                f"level is unchanged — labels and content patterns still apply"
            )
        level = self._provenance_default_level()
        return level, f"M365 provenance ({src}) -> {level} floor"

    # ── Sensitivity ────────────────────────────────────────────────────
    def classify_sensitivity(
        self, text: str, label: str | None = None, source: str | None = None
    ) -> tuple[str, list[str]]:
        """Return (level, signals) combining text scan, MIP label and provenance."""
        signals: list[str] = []
        level = NORMAL

        if text:
            for index, pattern in enumerate(self._restricted):
                match = pattern.search(text)
                if match:
                    level = RESTRICTED
                    signals.append(
                        f"restricted pattern #{index} matched, shape "
                        f"{_redact(match.group(0))!r}"
                    )
                    break
            if level != RESTRICTED:
                for index, pattern in enumerate(self._confidential):
                    match = pattern.search(text)
                    if match:
                        level = CONFIDENTIAL
                        signals.append(
                            f"confidential pattern #{index} matched, shape "
                            f"{_redact(match.group(0))!r}"
                        )
                        break

        label_level = normalize_label(label)
        if label_level != NORMAL:
            signals.append(f"MIP label {label!r} -> {label_level}")
        level = max_sensitivity(level, label_level)

        src = (source or "").strip().lower()
        if src:
            prov_level, explanation = self._provenance_level(src)
            if explanation:
                signals.append(explanation)
            if prov_level:
                level = max_sensitivity(level, prov_level)

        return level, signals

    # ── Difficulty ─────────────────────────────────────────────────────
    def classify_difficulty(self, text: str) -> str:
        if not text or not text.strip():
            return STANDARD
        body = text.strip()
        words = body.split()
        cfg = self._diff_cfg

        if cfg.get("hard_if_code_block", True) and "```" in body:
            return HARD
        for pattern in self._hard:
            if pattern.search(body):
                return HARD
        if len(body) > cfg.get("hard_if_long_input", 600):
            return HARD
        if len(words) > cfg.get("hard_if_many_words", 80):
            return HARD
        if body.count("\n") >= cfg.get("hard_if_many_lines", 8):
            return HARD
        for pattern in self._simple:
            if pattern.search(body):
                return SIMPLE
        if len(words) <= cfg.get("simple_if_short_words", 4):
            return SIMPLE
        return STANDARD

    # ── Role ───────────────────────────────────────────────────────────
    def classify_role(self, text: str) -> str:
        """Highest-scoring role by whole-word cue matches; ties break to general.

        Cues match on word boundaries. The Hermes original uses bare substring
        matching, so the cue "test" fires on "latest", "contest" and "greatest"
        and misroutes ordinary prose to the coding model.
        """
        if not text or not text.strip():
            return "general"
        body = text.strip()
        best_role, best_score = "general", 0
        for role_name, patterns in sorted(self._role_cues.items()):
            score = sum(1 for pattern in patterns if pattern.search(body))
            if score > best_score:
                best_role, best_score = role_name, score
        return best_role

    # ── Combined ───────────────────────────────────────────────────────
    def classify(
        self, text: str, label: str | None = None, source: str | None = None
    ) -> Classification:
        sensitivity, signals = self.classify_sensitivity(text, label=label, source=source)
        difficulty = self.classify_difficulty(text)
        role = self.classify_role(text)
        tier = DIFFICULTY_TO_TIER.get(difficulty, "balanced")

        ceiling = SENSITIVITY_CEILING[sensitivity]
        raw_cap = (self._cfg.get("policy", {}) or {}).get("max_egress")
        if raw_cap not in (None, ""):
            cap = normalize_egress(raw_cap)
            if cap is None:
                # An unreadable cap must not silently disable the policy. Fail
                # closed and say so; `validate()` reports it as a config error.
                cap = ON_DEVICE
                signals.append(
                    f"policy max_egress={raw_cap!r} is not a valid egress class; "
                    f"failing closed to {ON_DEVICE!r}"
                )
            else:
                signals.append(f"policy max_egress={cap}")
            ceiling = tighter(ceiling, cap)

        return Classification(
            sensitivity=sensitivity,
            role=role,
            difficulty=difficulty,
            tier=tier,
            ceiling=ceiling,
            signals=signals,
        )


def _compile_cue(cue: str) -> re.Pattern:
    """Compile a literal cue with word boundaries where the edges are wordy."""
    escaped = re.escape(cue)
    prefix = r"\b" if cue[:1].isalnum() else ""
    suffix = r"\b" if cue[-1:].isalnum() else ""
    return re.compile(f"{prefix}{escaped}{suffix}", re.IGNORECASE)


def _redact(fragment: str, keep: int = 40) -> str:
    """Describe a match's *shape* without reproducing any of its characters.

    Truncation is not enough: the SSN pattern matches exactly 11 characters, so
    echoing "the first 12" reprints the whole thing verbatim. Nor is masking
    only alphanumerics — a symbol-only credential like `secret=+-*/` survives
    intact. Every non-space character is replaced: digits with `#`, letters
    with `x`, everything else with `.`, which keeps the shape recognisable
    (`###.##.####` is clearly an SSN) while leaking nothing.
    """
    collapsed = " ".join(fragment.split())
    masked = []
    for char in collapsed[:keep]:
        if char.isdigit():
            masked.append("#")
        elif char.isalpha():
            masked.append("x")
        elif char == " ":
            masked.append(" ")
        else:
            masked.append(".")
    suffix = "..." if len(collapsed) > keep else ""
    return "".join(masked) + suffix
