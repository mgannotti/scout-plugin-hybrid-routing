"""Command line interface — the /route equivalent for Scout."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .backends import BackendError, get_backend, probe
from .config import ConfigError, install_user_config, load_config
from .router import ROUTE_BLOCKED, ROUTE_OK, ROUTE_UNCONFIGURED, HybridRouter
from .selftest import run_selftest

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_UNCONFIGURED = 2
EXIT_ERROR = 3


def _router(args) -> tuple[HybridRouter, Path]:
    config, path = load_config(getattr(args, "config", None))
    return HybridRouter(config), path


def _emit(payload: dict, as_json: bool, renderer) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        renderer(payload)


# ── classify ───────────────────────────────────────────────────────────
def cmd_classify(args) -> int:
    router, path = _router(args)
    text = " ".join(args.text) if args.text else ""
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    if not text.strip():
        print("error: no text supplied", file=sys.stderr)
        return EXIT_ERROR

    decision = router.route(text, label=args.label, source=args.source)
    payload = decision.to_dict()
    payload["config_path"] = str(path)
    _emit(payload, args.json, _render_decision)

    if decision.status == ROUTE_BLOCKED:
        return EXIT_BLOCKED
    if decision.status == ROUTE_UNCONFIGURED:
        return EXIT_UNCONFIGURED
    return EXIT_OK


def _render_decision(d: dict) -> None:
    status = d["status"]
    banner = {
        ROUTE_OK: "ROUTING DECISION",
        ROUTE_BLOCKED: "ROUTING BLOCKED",
        ROUTE_UNCONFIGURED: "NOT CONFIGURED",
    }.get(status, status.upper())
    print()
    print("=" * 66)
    print(f"  {banner}")
    print("=" * 66)
    print()
    print(f"  sensitivity   : {d['sensitivity']}")
    print(f"  egress ceiling: {d['egress_ceiling']}")
    print(f"  role          : {d['role']}")
    print(f"  difficulty    : {d['difficulty']}  (tier {d['tier']})")
    print()
    if d["model"]:
        print(f"  MODEL         : {d['model']}")
        print(f"  egress        : {d['egress']}")
        print(f"  channel       : {d['channel']}")
        if d.get("reasoning_effort"):
            print(f"  effort        : {d['reasoning_effort']}")
        print(f"  delegate      : {'YES' if d['should_delegate'] else 'NO — handle inline'}")
    else:
        print("  MODEL         : (none selected)")
    print()
    print(f"  reason: {d['reason']}")

    if d.get("signals"):
        print()
        print("  signals:")
        for signal in d["signals"]:
            print(f"    - {signal}")

    execution = d.get("execution") or {}
    if execution.get("call"):
        call = execution["call"]
        args_text = ", ".join(f'{k}="{v}"' for k, v in call.items() if k != "tool")
        print()
        print(f"  run: {call['tool']}({args_text}, prompt=...)")
    elif execution.get("cli"):
        print()
        print(f"  run: {execution['cli']}")

    if d.get("fallback_chain"):
        print()
        print("  fallback chain (all within ceiling):")
        for i, candidate in enumerate(d["fallback_chain"], 1):
            print(f"    {i}. {candidate['ref']}  [{candidate['egress']}]  via {candidate['origin']}")

    if d.get("rejected_for_egress"):
        print()
        print("  excluded (outside ceiling):")
        for item in d["rejected_for_egress"]:
            print(f"    x {item['ref']}  — {item['why']}")
    print()


# ── status ─────────────────────────────────────────────────────────────
def cmd_status(args) -> int:
    router, path = _router(args)
    payload = router.status()
    payload["config_path"] = str(path)
    _emit(payload, args.json, _render_status)
    return EXIT_OK if payload["configured"] else EXIT_UNCONFIGURED


def _render_status(s: dict) -> None:
    print()
    print("=" * 66)
    print("  HYBRID CONTEXTUAL INFERENCE — configuration")
    print("=" * 66)
    print()
    print(f"  config    : {s.get('config_path', '—')}")
    print(f"  configured: {'yes' if s['configured'] else 'NO — all model fields blank'}")
    policy = (s.get("policy") or {}).get("max_egress") or "(none)"
    print(f"  policy cap: {policy}")
    print(f"  backends  : {', '.join(s.get('backends') or []) or '(none)'}")
    print()
    if s["models"]:
        print("  MODELS")
        width = max(len(m["origin"]) for m in s["models"])
        for entry in s["models"]:
            if entry.get("error"):
                print(f"    {entry['origin']:<{width}}  {entry['ref']}   !! {entry['error']}")
            else:
                print(
                    f"    {entry['origin']:<{width}}  {entry['ref']}"
                    f"   [{entry['egress']} via {entry['channel']}]"
                )
    else:
        print("  MODELS      (none configured)")
    if s["problems"]:
        print()
        print("  PROBLEMS")
        for problem in s["problems"]:
            print(f"    ! {problem}")
    print()


# ── probe ──────────────────────────────────────────────────────────────
def cmd_probe(args) -> int:
    try:
        config, _ = load_config(getattr(args, "config", None))
    except ConfigError:
        config = {}
    results = probe(config, include_known=not args.configured_only)
    if args.json:
        print(json.dumps(results, indent=2))
        return EXIT_OK
    print()
    print("=" * 66)
    print("  BACKEND PROBE")
    print("=" * 66)
    print()
    if not results:
        print("  No backends configured and no known local runtime reachable.")
        print("  Install one (Ollama, LM Studio, Foundry Local) or configure an")
        print("  org-hosted endpoint to enable the local half of the stack.")
        print()
        return EXIT_UNCONFIGURED
    for entry in results:
        mark = "up  " if entry["reachable"] else "down"
        print(f"  [{mark}] {entry['backend']:<22} {entry['base_url']}  ({entry['egress']})")
        if entry["reachable"] and entry["models"]:
            for model in entry["models"][:12]:
                print(f"           - {model}")
            if len(entry["models"]) > 12:
                print(f"           ... and {len(entry['models']) - 12} more")
        elif not entry["reachable"]:
            print(f"           {entry.get('error', '')[:100]}")
    print()
    return EXIT_OK


# ── infer ──────────────────────────────────────────────────────────────
def cmd_infer(args) -> int:
    config, _ = load_config(getattr(args, "config", None))
    prompt = args.prompt or ""
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    if not prompt.strip():
        print("error: no prompt supplied", file=sys.stderr)
        return EXIT_ERROR

    system = args.system or ""
    auth = HybridRouter(config).authorize_inference(
        args.backend,
        f"{system}\n{prompt}".strip(),
        label=getattr(args, "label", None),
        source=getattr(args, "source", None),
    )
    if not auth["allowed"]:
        if args.json:
            print(json.dumps({"error": auth["reason"], "authorization": auth}, indent=2))
        else:
            print(f"\n{auth['reason']}\n", file=sys.stderr)
            for conflict in auth.get("conflicts", []):
                print(f"  ! {conflict}", file=sys.stderr)
        return EXIT_BLOCKED

    try:
        backend = get_backend(config, args.backend)
        result = backend.chat(
            model=args.model,
            prompt=prompt,
            system=args.system,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
        )
    except BackendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        result["authorization"] = auth
        print(json.dumps(result, indent=2))
    else:
        print(result["content"])
    return EXIT_OK


# ── init / test ────────────────────────────────────────────────────────
def cmd_init(args) -> int:
    path = install_user_config(overwrite=args.force)
    print(f"config ready at: {path}")
    print("Edit it to fill in your models, then run: python -m hybrid_routing status")
    return EXIT_OK


def cmd_test(args) -> int:
    results = run_selftest(getattr(args, "config", None))
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print()
        print("=" * 66)
        print(f"  SELF-TEST — {results['passed']}/{results['total']} passed")
        print("=" * 66)
        print()
        for case in results["cases"]:
            mark = "PASS" if case["passed"] else "FAIL"
            print(f"  [{mark}] {case['name']}")
            if not case["passed"]:
                print(f"         expected {case['expected']}")
                print(f"         actual   {case['actual']}")
        print()
    return EXIT_OK if results["passed"] == results["total"] else EXIT_BLOCKED


# ── parser ─────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hybrid_routing",
        description="Hybrid contextual inference routing for Microsoft Scout.",
    )
    parser.add_argument("--config", help="path to routing_config.yaml")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    p_classify = sub.add_parser("classify", help="classify a task and route it")
    p_classify.add_argument("text", nargs="*", help="task text")
    p_classify.add_argument("--file", help="read task text from a file")
    p_classify.add_argument("--label", help="MIP sensitivity label, e.g. 'Highly Confidential'")
    p_classify.add_argument(
        "--source",
        help="provenance: email, teams, calendar, sharepoint, onedrive, web, local",
    )
    p_classify.set_defaults(func=cmd_classify)

    sub.add_parser("status", help="show the routing configuration").set_defaults(func=cmd_status)

    p_probe = sub.add_parser("probe", help="check which backends are reachable")
    p_probe.add_argument(
        "--configured-only", action="store_true", help="skip well-known port discovery"
    )
    p_probe.set_defaults(func=cmd_probe)

    p_infer = sub.add_parser("infer", help="run a prompt on a local/org backend")
    p_infer.add_argument("--backend", required=True)
    p_infer.add_argument("--model", required=True)
    p_infer.add_argument("--prompt")
    p_infer.add_argument("--prompt-file")
    p_infer.add_argument("--system")
    p_infer.add_argument("--label", help="MIP sensitivity label of the prompt content")
    p_infer.add_argument("--source", help="provenance of the prompt content")
    p_infer.add_argument("--temperature", type=float, default=0.2)
    p_infer.add_argument("--max-tokens", type=int)
    p_infer.set_defaults(func=cmd_infer)

    p_init = sub.add_parser("init", help="install the user config")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing config")
    p_init.set_defaults(func=cmd_init)

    sub.add_parser("test", help="run the routing self-test").set_defaults(func=cmd_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
