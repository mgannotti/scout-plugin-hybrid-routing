"""Egress classes — where a model's inference actually happens.

This is the concept the Hermes original lacks. There, a model reference is an
opaque `provider/model-id` string and "local" is a naming convention. Nothing
stops the fallback chain from handing sensitive content to a cloud endpoint.

Here every model reference resolves to an *egress class* describing the trust
boundary its tokens cross:

    on-device    tokens never leave the machine
    org-tenant   tokens stay inside the org's tenant / network boundary
    cloud-public tokens go to a public multi-tenant provider

Routing policy is expressed as a *ceiling*: the least-trusted boundary a task
is permitted to cross. Selection and fallback are both filtered by the ceiling,
so a sensitive task can never fall through to a cloud model.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

ON_DEVICE = "on-device"
ORG_TENANT = "org-tenant"
CLOUD_PUBLIC = "cloud-public"

# Lower rank == tighter boundary. A model is permitted when its rank is <= the
# ceiling's rank.
_RANK = {ON_DEVICE: 0, ORG_TENANT: 1, CLOUD_PUBLIC: 2}

EGRESS_CLASSES = (ON_DEVICE, ORG_TENANT, CLOUD_PUBLIC)

# Execution channels — how the caller actually runs the model.
CHANNEL_SCOUT_TASK = "scout-task"  # Scout's built-in `task` tool with model=
CHANNEL_OPENAI_HTTP = "openai-http"  # OpenAI-compatible /v1/chat/completions

# Scheme prefixes used in model references.
SCHEME_SCOUT = "scout"
SCHEME_LOCAL = "local"
SCHEME_ORG = "org"

_SCHEME_EGRESS = {
    SCHEME_SCOUT: CLOUD_PUBLIC,
    SCHEME_LOCAL: ON_DEVICE,
    SCHEME_ORG: ORG_TENANT,
}

_SCHEME_CHANNEL = {
    SCHEME_SCOUT: CHANNEL_SCOUT_TASK,
    SCHEME_LOCAL: CHANNEL_OPENAI_HTTP,
    SCHEME_ORG: CHANNEL_OPENAI_HTTP,
}


def rank(egress_class: str) -> int:
    """Rank of an egress class. Unknown classes are treated as least trusted."""
    return _RANK.get(egress_class, _RANK[CLOUD_PUBLIC])


def permits(ceiling: str, egress_class: str) -> bool:
    """True when `egress_class` is at or inside the `ceiling` boundary."""
    return rank(egress_class) <= rank(ceiling)


def tighter(a: str, b: str) -> str:
    """Return whichever of the two ceilings is more restrictive."""
    return a if rank(a) <= rank(b) else b


def looser(a: str, b: str) -> str:
    """Return whichever of the two egress classes is LESS trusted."""
    return a if rank(a) >= rank(b) else b


def normalize_egress(value: str | None) -> str | None:
    """Normalize an egress class name. Returns None if unrecognized.

    Case and surrounding whitespace are forgiven; anything else is rejected
    rather than guessed, so a typo surfaces as a config error instead of
    silently disabling a policy cap.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return text if text in EGRESS_CLASSES else None


# Hosts that prove inference is happening on this machine.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})


def url_is_loopback(url: str) -> bool:
    """True when a base URL points at this machine."""
    try:
        host = urlparse(url).hostname
    except ValueError:
        return False
    if host is None:
        return False
    host = host.strip("[]").lower()
    return host in LOOPBACK_HOSTS or host.endswith(".localhost")



class ModelRefError(ValueError):
    """Raised when a model reference cannot be parsed."""


@dataclass(frozen=True)
class ModelRef:
    """A parsed model reference.

    Accepted forms::

        scout/<model-id>              cloud, run via Scout's `task` tool
        local/<runtime>/<model-id>    on-device OpenAI-compatible endpoint
        org/<endpoint>/<model-id>     org-hosted OpenAI-compatible endpoint

    `runtime` / `endpoint` names a backend entry in the config, which supplies
    the base URL and any auth. Keeping the endpoint out of the model reference
    means the reference itself never carries a credential.
    """

    raw: str
    scheme: str
    backend: str
    model_id: str

    @property
    def egress(self) -> str:
        return _SCHEME_EGRESS[self.scheme]

    @property
    def channel(self) -> str:
        return _SCHEME_CHANNEL[self.scheme]

    @property
    def is_local(self) -> bool:
        return self.scheme == SCHEME_LOCAL

    @property
    def is_cloud(self) -> bool:
        return self.scheme == SCHEME_SCOUT

    def to_dict(self) -> dict:
        return {
            "ref": self.raw,
            "scheme": self.scheme,
            "backend": self.backend,
            "model_id": self.model_id,
            "egress": self.egress,
            "channel": self.channel,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.raw


def parse_model_ref(ref: str) -> ModelRef:
    """Parse a model reference string into a :class:`ModelRef`.

    Raises :class:`ModelRefError` for anything unparseable. We deliberately do
    not guess: an unrecognized scheme could be local or cloud, and guessing
    wrong on that question is exactly the failure this module exists to
    prevent.
    """
    if not ref or not ref.strip():
        raise ModelRefError("empty model reference")

    text = ref.strip()
    parts = text.split("/")
    scheme = parts[0].lower()

    if scheme not in _SCHEME_EGRESS:
        raise ModelRefError(
            f"unknown scheme {parts[0]!r} in model reference {ref!r}. "
            f"Expected one of: scout/<model>, local/<runtime>/<model>, "
            f"org/<endpoint>/<model>."
        )

    if scheme == SCHEME_SCOUT:
        if len(parts) != 2 or not parts[1].strip():
            raise ModelRefError(
                f"malformed cloud reference {ref!r}; expected scout/<model-id>"
            )
        return ModelRef(raw=text, scheme=scheme, backend="scout", model_id=parts[1].strip())

    if len(parts) < 3 or not parts[1].strip() or not "/".join(parts[2:]).strip():
        raise ModelRefError(
            f"malformed reference {ref!r}; expected {scheme}/<backend>/<model-id>"
        )

    # Model ids may themselves contain slashes (e.g. org/foundry/publisher/model).
    return ModelRef(
        raw=text,
        scheme=scheme,
        backend=parts[1].strip(),
        model_id="/".join(parts[2:]).strip(),
    )


@dataclass(frozen=True)
class ResolvedEgress:
    """The egress class a reference *actually* has, once its backend is known.

    A reference's scheme is a claim, not a fact. `local/ollama/qwen3` says
    on-device, but the bytes go wherever `backends.ollama.base_url` points. If
    those two disagree, trusting the scheme sends sensitive content to whatever
    the URL names — the exact failure this module exists to prevent.

    So the effective class is the LEAST trusted of every available signal, and
    any disagreement is recorded as a conflict for `validate()` to surface.
    """

    egress: str
    conflicts: list[str]

    @property
    def ok(self) -> bool:
        return not self.conflicts


def resolve_backend_egress(name: str, backends: dict | None) -> ResolvedEgress:
    """Determine a backend's trust level from its own config alone.

    Used when there is no model reference to consult — notably when executing
    an inference request directly, where the caller names a backend rather than
    a routed model.
    """
    backends = backends or {}
    entry = backends.get(name)
    conflicts: list[str] = []

    if entry is None:
        return ResolvedEgress(
            egress=CLOUD_PUBLIC,
            conflicts=[f"backend {name!r} is not defined under `backends:`."],
        )

    declared = normalize_egress((entry or {}).get("egress"))
    if declared is None:
        raw = (entry or {}).get("egress")
        detail = (
            f"declares egress {raw!r}, which is not one of {', '.join(EGRESS_CLASSES)}"
            if raw is not None
            else "does not declare an `egress:` class"
        )
        conflicts.append(f"backend {name!r} {detail}; treating it as {CLOUD_PUBLIC!r}.")
        declared = CLOUD_PUBLIC

    effective = declared
    base_url = str((entry or {}).get("base_url", "") or "").strip()

    if not base_url:
        conflicts.append(f"backend {name!r} has no base_url.")
        effective = looser(effective, CLOUD_PUBLIC)
    else:
        if rank(effective) == rank(ON_DEVICE) and not url_is_loopback(base_url):
            conflicts.append(
                f"backend {name!r} claims {ON_DEVICE!r} but {base_url!r} is not a "
                f"loopback address; treating it as {ORG_TENANT!r} at best."
            )
            effective = looser(effective, ORG_TENANT)
        if not url_is_loopback(base_url) and not base_url.lower().startswith("https://"):
            conflicts.append(
                f"backend {name!r} uses a non-loopback cleartext URL {base_url!r}. "
                f"Prompts and API keys would cross the network unencrypted."
            )
            effective = looser(effective, CLOUD_PUBLIC)

    return ResolvedEgress(egress=effective, conflicts=conflicts)


def resolve_egress(ref: ModelRef, backends: dict | None) -> ResolvedEgress:
    """Reconcile a reference's claimed egress with its backend's reality."""
    backends = backends or {}
    effective = ref.egress
    conflicts: list[str] = []

    # Scout cloud models have no backend entry; the scheme is authoritative.
    if ref.scheme == SCHEME_SCOUT:
        return ResolvedEgress(egress=effective, conflicts=conflicts)

    entry = backends.get(ref.backend)
    if entry is None:
        conflicts.append(
            f"{ref.raw!r} names backend {ref.backend!r}, which is not defined "
            f"under `backends:`. Its real destination is unknown."
        )
        return ResolvedEgress(egress=CLOUD_PUBLIC, conflicts=conflicts)

    declared_raw = (entry or {}).get("egress")
    declared = normalize_egress(declared_raw)
    if declared is None:
        # A missing or unreadable egress is not evidence of trust. Without it
        # the only thing we know is the URL, so refuse to inherit the scheme's
        # claim. Previously a backend with no `egress:` key silently kept the
        # scheme's trust level, so `org/tenant/m` pointing at a public API was
        # accepted for confidential content.
        detail = (
            f"declares egress {declared_raw!r}, which is not one of "
            f"{', '.join(EGRESS_CLASSES)}"
            if declared_raw is not None
            else "does not declare an `egress:` class"
        )
        conflicts.append(
            f"backend {ref.backend!r} {detail}. Its trust level cannot be "
            f"established, so {ref.raw!r} is treated as {CLOUD_PUBLIC!r}."
        )
        effective = looser(effective, CLOUD_PUBLIC)
    elif declared != ref.egress:
        conflicts.append(
            f"{ref.raw!r} claims {ref.egress!r} via its scheme, but backend "
            f"{ref.backend!r} declares egress {declared!r}. Using the less "
            f"trusted of the two."
        )
        effective = looser(effective, declared)

    # An on-device claim is only credible if the endpoint is on this machine.
    base_url = str((entry or {}).get("base_url", "") or "").strip()
    if not base_url:
        conflicts.append(f"backend {ref.backend!r} has no base_url.")
        effective = looser(effective, CLOUD_PUBLIC)
    elif rank(effective) == rank(ON_DEVICE) and not url_is_loopback(base_url):
        conflicts.append(
            f"{ref.raw!r} resolves to {ref.backend!r} at {base_url!r}, which is "
            f"not a loopback address, so it cannot be {ON_DEVICE!r}. Treating it "
            f"as {ORG_TENANT!r} at best."
        )
        effective = looser(effective, ORG_TENANT)

    # Anything leaving this machine must be encrypted. Confidential prompts and
    # the Authorization header would otherwise cross the network in cleartext.
    if base_url and not url_is_loopback(base_url) and not base_url.lower().startswith("https://"):
        conflicts.append(
            f"backend {ref.backend!r} uses a non-loopback cleartext URL "
            f"{base_url!r}. Prompts and API keys would cross the network "
            f"unencrypted; use https://."
        )
        effective = looser(effective, CLOUD_PUBLIC)

    return ResolvedEgress(egress=effective, conflicts=conflicts)

