"""Backends — actually running on-device and org-hosted models.

The Hermes plugin only *recommends* a model; Hermes' own provider layer runs
it. Scout has no such layer for non-Scout models: its `task` tool accepts only
Scout's hosted cloud models. So for the local and org-hosted half of a hybrid
stack this module has to do the work itself.

Every supported runtime speaks the OpenAI chat-completions wire format, so one
client covers all of them:

    Ollama         http://localhost:11434/v1
    LM Studio      http://localhost:1234/v1
    Foundry Local  http://localhost:5273/v1
    llama.cpp      http://localhost:8080/v1
    vLLM           http://localhost:8000/v1
    Azure AI Foundry / any org-hosted OpenAI-compatible endpoint

Standard library only — no extra dependency for the network path.

API keys are never stored in the config. A backend names an environment
variable via `api_key_env` and the key is read from the process environment at
call time.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .egress import CLOUD_PUBLIC, normalize_egress, url_is_loopback

DEFAULT_TIMEOUT = 120

# Well-known local runtimes, probed by `probe` when autodetecting.
KNOWN_LOCAL_RUNTIMES = {
    "ollama": "http://localhost:11434/v1",
    "lmstudio": "http://localhost:1234/v1",
    "foundry": "http://localhost:5273/v1",
    "llamacpp": "http://localhost:8080/v1",
    "vllm": "http://localhost:8000/v1",
    "jan": "http://localhost:1337/v1",
}


class BackendError(RuntimeError):
    """Raised when a backend is misconfigured or unreachable."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses redirects. A 302 from a loopback endpoint could point anywhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError(
            f"refusing redirect to {newurl!r}: an on-device request must not "
            f"leave the machine"
        )


@dataclass
class Backend:
    """A configured OpenAI-compatible endpoint."""

    name: str
    base_url: str
    egress: str = CLOUD_PUBLIC
    api_key_env: str = ""
    timeout: int = DEFAULT_TIMEOUT
    extra_headers: dict = field(default_factory=dict)

    @classmethod
    def from_config(cls, name: str, cfg: dict) -> "Backend":
        cfg = cfg or {}
        base = str(cfg.get("base_url", "") or "").strip().rstrip("/")
        if not base:
            raise BackendError(f"backend {name!r} has no base_url")
        # Default to the LEAST trusted class. A backend that forgets to declare
        # its egress must not be assumed on-device.
        declared = normalize_egress(cfg.get("egress")) or CLOUD_PUBLIC
        try:
            timeout = int(cfg.get("timeout", DEFAULT_TIMEOUT))
        except (TypeError, ValueError) as exc:
            raise BackendError(
                f"backend {name!r} has a non-numeric timeout {cfg.get('timeout')!r}"
            ) from exc
        return cls(
            name=name,
            base_url=base,
            egress=declared,
            api_key_env=str(cfg.get("api_key_env", "") or ""),
            timeout=timeout,
            extra_headers=dict(cfg.get("headers", {}) or {}),
        )

    @property
    def is_loopback(self) -> bool:
        return url_is_loopback(self.base_url)

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        headers.update(self.extra_headers)
        if self.api_key_env:
            key = os.environ.get(self.api_key_env, "").strip()
            if not key:
                raise BackendError(
                    f"backend {self.name!r} expects an API key in ${self.api_key_env}, "
                    f"which is unset"
                )
            headers["Authorization"] = f"Bearer {key}"
            headers.setdefault("api-key", key)  # Azure-style endpoints
        return headers

    # ── Calls ──────────────────────────────────────────────────────────
    def list_models(self, timeout: int | None = None) -> list[str]:
        payload = self._get("/models", timeout=timeout)
        entries = payload.get("data") or []
        return [str(entry.get("id", "")) for entry in entries if entry.get("id")]

    def chat(
        self,
        model: str,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout: int | None = None,
    ) -> dict:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body: dict = {"model": model, "messages": messages, "temperature": temperature}
        if max_tokens:
            body["max_tokens"] = max_tokens

        payload = self._post("/chat/completions", body, timeout=timeout)
        choices = payload.get("choices") or []
        content = ""
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""
        return {
            "backend": self.name,
            "egress": self.egress,
            "model": payload.get("model", model),
            "content": content,
            "usage": payload.get("usage", {}),
        }

    def health(self, timeout: int = 3) -> dict:
        try:
            models = self.list_models(timeout=timeout)
            return {
                "backend": self.name,
                "base_url": self.base_url,
                "egress": self.egress,
                "reachable": True,
                "models": models,
            }
        except Exception as exc:  # noqa: BLE001 - health check reports any failure
            return {
                "backend": self.name,
                "base_url": self.base_url,
                "egress": self.egress,
                "reachable": False,
                "error": str(exc),
                "models": [],
            }

    # ── Transport ──────────────────────────────────────────────────────
    def _get(self, path: str, timeout: int | None = None) -> dict:
        return self._request(path, None, timeout)

    def _post(self, path: str, body: dict, timeout: int | None = None) -> dict:
        return self._request(path, body, timeout)

    def _request(self, path: str, body: dict | None, timeout: int | None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url, data=data, headers=self._headers(), method="POST" if data else "GET"
        )
        try:
            with self._opener().open(request, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise BackendError(f"{self.name}: HTTP {exc.code} from {url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise BackendError(f"{self.name}: cannot reach {url}: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise BackendError(f"{self.name}: non-JSON response from {url}") from exc
        except UnicodeDecodeError as exc:
            raise BackendError(f"{self.name}: undecodable response from {url}") from exc

    def _opener(self) -> urllib.request.OpenerDirector:
        """Build an opener that cannot leak a loopback request off-machine.

        `urllib.request.urlopen` honours $HTTP_PROXY, and a proxy is not
        excluded for localhost unless $NO_PROXY says so. Without this, an
        on-device backend could hand a restricted prompt to an external proxy
        while still being reported as on-device. Redirects are refused for the
        same reason — a 302 could point anywhere.
        """
        if self.is_loopback:
            return urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _NoRedirect()
            )
        return urllib.request.build_opener()


def load_backends(config: dict) -> dict[str, Backend]:
    backends: dict[str, Backend] = {}
    for name, cfg in (config.get("backends", {}) or {}).items():
        try:
            backends[name] = Backend.from_config(name, cfg)
        except BackendError:
            continue
    return backends


def get_backend(config: dict, name: str) -> Backend:
    backends = load_backends(config)
    if name not in backends:
        known = ", ".join(sorted(backends)) or "none"
        raise BackendError(
            f"backend {name!r} is not defined in the config. Defined backends: {known}"
        )
    return backends[name]


def probe(config: dict | None = None, include_known: bool = True) -> list[dict]:
    """Report which backends are actually reachable right now.

    Covers configured backends plus, optionally, the well-known local runtime
    ports so a fresh install can discover what is already running.
    """
    results: list[dict] = []
    seen_urls: set[str] = set()

    for backend in load_backends(config or {}).values():
        results.append(backend.health())
        seen_urls.add(backend.base_url)

    if include_known:
        for name, url in KNOWN_LOCAL_RUNTIMES.items():
            if url.rstrip("/") in seen_urls:
                continue
            probe_backend = Backend(name=f"{name} (unconfigured)", base_url=url)
            health = probe_backend.health()
            health["configured"] = False
            if health["reachable"]:
                results.append(health)
    return results
