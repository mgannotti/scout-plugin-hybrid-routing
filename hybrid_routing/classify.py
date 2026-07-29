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
# when it carries no explicit label. Scout reads these surfaces routinely;
# treating them as public-cloud-safe by default would be wrong.
M365_SOURCES = frozenset(
    {"email", "teams", "calendar", "sharepoint", "onedrive", "planner", "todo", "transcript"}
)
PUBLIC_SOURCES = frozenset({"web", "user", "local", "repo", ""})

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

    def _compile_all(self, patterns, where: str) -> list[re.Pattern]:
        compiled = []
        for index, pattern in enumerate(patterns or []):
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except (re.error, TypeError) as exc:
                self.pattern_errors.append(f"{where}[{index}] is not a valid regex: {exc}")
        return compiled

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
        if src in M365_SOURCES:
            signals.append(f"M365 provenance ({src}) -> confidential floor")
            level = max_sensitivity(level, CONFIDENTIAL)

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
