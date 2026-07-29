"""MCP stdio server — exposes the router as native Scout tools.

Hermes registers `route_classify` / `route_status` / `route_test` through its
plugin API. Scout has no Python plugin hook, so the equivalent surface is an
MCP server: once registered, these tools are in the model's tool list on every
turn, with no skill invocation needed.

Implemented as plain JSON-RPC over stdin/stdout against the MCP wire protocol,
so the only dependency is PyYAML (already needed for the config).

Register with::

    {
      "name": "hybrid-routing",
      "type": "stdio",
      "command": "python",
      "args": ["-m", "hybrid_routing.mcp_server"],
      "env": {"PYTHONPATH": "<path to this repo>"}
    }
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from .backends import BackendError, get_backend, probe
from .config import ConfigError, load_config
from .router import HybridRouter
from .selftest import run_selftest

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "hybrid-routing", "version": "1.2.0"}

TOOLS = [
    {
        "name": "route_classify",
        "description": (
            "Classify a task by sensitivity, role and difficulty, then return which "
            "model should handle it, whether to delegate, and how to execute it. "
            "Enforces an egress boundary: content classified confidential or restricted "
            "can only be routed to org-hosted or on-device models, and is BLOCKED "
            "rather than downgraded to a cloud model if none is configured. Pass "
            "`label` when the content carries a Microsoft Information Protection "
            "sensitivity label, and `source` when it came from an M365 surface."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The task text to classify."},
                "label": {
                    "type": "string",
                    "description": "MIP sensitivity label, e.g. 'Highly Confidential'.",
                },
                "source": {
                    "type": "string",
                    "description": (
                        "Where the content came from: email, teams, calendar, "
                        "sharepoint, onedrive, transcript, web, local."
                    ),
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "route_status",
        "description": (
            "Show the hybrid routing configuration — which model is assigned to each "
            "tier, role and sensitivity level, the egress class of each, any config "
            "problems, and the configured backends."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "route_test",
        "description": (
            "Run the routing self-test against a fixed reference config to verify the "
            "classification and egress-enforcement engine behaves correctly."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "route_probe",
        "description": (
            "Report which local and org-hosted inference backends are reachable right "
            "now, and which models each is serving. Also discovers well-known local "
            "runtime ports (Ollama, LM Studio, Foundry Local, llama.cpp, vLLM) that "
            "are running but not yet configured."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "configured_only": {
                    "type": "boolean",
                    "description": "Skip discovery of well-known ports.",
                }
            },
        },
    },
    {
        "name": "route_infer",
        "description": (
            "Run a prompt on a local or org-hosted backend over its OpenAI-compatible "
            "API. This is how content that route_classify sent to an on-device or "
            "org-tenant model actually gets executed — Scout's own task tool can only "
            "host cloud models. Use the backend and model from the routing decision. "
            "The prompt is re-classified here and REFUSED if the backend's resolved "
            "egress exceeds what the content permits, so this cannot be used to "
            "bypass the routing policy."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "backend": {"type": "string", "description": "Configured backend name."},
                "model": {"type": "string", "description": "Model id on that backend."},
                "prompt": {"type": "string", "description": "The user prompt."},
                "system": {"type": "string", "description": "Optional system prompt."},
                "label": {
                    "type": "string",
                    "description": "MIP sensitivity label of the prompt content.",
                },
                "source": {
                    "type": "string",
                    "description": "Provenance of the prompt content.",
                },
                "temperature": {"type": "number"},
                "max_tokens": {"type": "integer"},
            },
            "required": ["backend", "model", "prompt"],
        },
    },
]


def _load() -> dict:
    config, _ = load_config(None)
    return config


# ── Tool implementations ───────────────────────────────────────────────
def _tool_route_classify(args: dict) -> dict:
    config = _load()
    router = HybridRouter(config)
    decision = router.route(
        args.get("text", ""),
        label=args.get("label"),
        source=args.get("source"),
    )
    return decision.to_dict()


def _tool_route_status(_args: dict) -> dict:
    config, path = load_config(None)
    status = HybridRouter(config).status()
    status["config_path"] = str(path)
    return status


def _tool_route_test(_args: dict) -> dict:
    return run_selftest()


def _tool_route_probe(args: dict) -> dict:
    try:
        config = _load()
    except ConfigError:
        config = {}
    return {"backends": probe(config, include_known=not args.get("configured_only", False))}


def _tool_route_infer(args: dict) -> dict:
    config = _load()
    prompt = args.get("prompt", "")
    system = args.get("system") or ""

    # Enforce the egress policy here, not just at routing time. A routing
    # decision that nothing checks at the point of transmission is advice, not
    # a control.
    auth = HybridRouter(config).authorize_inference(
        args["backend"],
        f"{system}\n{prompt}".strip(),
        label=args.get("label"),
        source=args.get("source"),
    )
    if not auth["allowed"]:
        return {"error": auth["reason"], "authorization": auth}

    backend = get_backend(config, args["backend"])
    result = backend.chat(
        model=args["model"],
        prompt=prompt,
        system=args.get("system"),
        temperature=float(args.get("temperature", 0.2)),
        max_tokens=args.get("max_tokens"),
    )
    result["authorization"] = auth
    return result


HANDLERS = {
    "route_classify": _tool_route_classify,
    "route_status": _tool_route_status,
    "route_test": _tool_route_test,
    "route_probe": _tool_route_probe,
    "route_infer": _tool_route_infer,
}


# ── JSON-RPC plumbing ──────────────────────────────────────────────────
def _result(request_id, payload) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _text_content(payload) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]}


def handle(message: dict) -> dict | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            return _error(request_id, -32601, f"unknown tool: {name}")
        try:
            return _result(request_id, _text_content(handler(args)))
        except (ConfigError, BackendError) as exc:
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": json.dumps({"error": str(exc)}, indent=2)}],
                    "isError": True,
                },
            )
        except Exception as exc:  # noqa: BLE001
            detail = f"{exc.__class__.__name__}: {exc}"
            return _result(
                request_id,
                {
                    "content": [
                        {"type": "text", "text": json.dumps({"error": detail}, indent=2)}
                    ],
                    "isError": True,
                },
            )

    if request_id is None:
        return None
    return _error(request_id, -32601, f"unknown method: {method}")


def serve(stdin=None, stdout=None) -> int:
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            response = handle(message)
        except Exception:  # noqa: BLE001 - never let the loop die
            traceback.print_exc(file=sys.stderr)
            response = _error(message.get("id"), -32603, "internal error")
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
    return 0


def main() -> int:  # pragma: no cover
    # Ensure the repo root is importable when launched as a bare script.
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return serve()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


