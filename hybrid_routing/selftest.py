"""Self-test — verifies the routing engine against a fixed reference config.

The Hermes original runs its test suite against whatever config happens to be
loaded, so the expected results depend on the user's own model choices; with a
plausible config it reports 6/9 through no fault of the user. Here the suite
pins its own reference config so a failure always means the engine changed.

`--config` is accepted only to additionally validate the user's live config,
which is reported separately.
"""

from __future__ import annotations

import copy

import yaml

from .config import DEFAULT_CONFIG, load_config
from .router import ROUTE_BLOCKED, ROUTE_OK, HybridRouter

# A complete hybrid stack: on-device fast tier, cloud balanced/strong,
# on-device restricted, org-tenant confidential.
_REFERENCE_OVERRIDES = {
    ("tiers", "fast", "model"): "local/ollama/phi4-mini",
    ("tiers", "balanced", "model"): "scout/cloud-balanced",
    ("tiers", "strong", "model"): "scout/cloud-strong",
    ("roles", "coding", "model"): "scout/cloud-coder",
    ("roles", "strategy", "model"): "scout/cloud-strong",
    ("roles", "creative", "model"): "scout/cloud-writer",
    ("sensitivity", "restricted_model"): "local/ollama/qwen3:8b",
    ("sensitivity", "confidential_model"): "org/tenant/gpt-4o",
    ("delegation", "primary_model"): "scout/cloud-balanced",
}


def reference_config(**overrides) -> dict:
    """The shipped default config with a full hybrid stack filled in."""
    with open(DEFAULT_CONFIG, "r", encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}
    cfg = copy.deepcopy(cfg)
    # The shipped default ships the org-hosted backend commented out, so the
    # reference stack has to define one for `org/tenant/...` to resolve.
    cfg.setdefault("backends", {})["tenant"] = {
        "base_url": "https://reference.services.ai.azure.com/openai/v1",
        "egress": "org-tenant",
    }
    for path, value in _REFERENCE_OVERRIDES.items():
        node = cfg
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
    for dotted, value in overrides.items():
        node = cfg
        keys = dotted.split("__")
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value
    return cfg


def _case(name, text, expect, label=None, source=None, config=None):
    return {
        "name": name,
        "text": text,
        "label": label,
        "source": source,
        "expect": expect,
        "config": config,
    }


def _cases() -> list[dict]:
    # A cloud-only stack, used to prove sensitive content is blocked rather
    # than downgraded when no in-boundary model exists.
    cloud_only = reference_config()
    cloud_only["sensitivity"]["restricted_model"] = ""
    cloud_only["sensitivity"]["confidential_model"] = ""
    cloud_only["tiers"]["fast"]["model"] = "scout/cloud-fast"

    locked_down = reference_config()
    locked_down["policy"]["max_egress"] = "on-device"

    # A cloud fast tier, to exercise inline handling. `skip_for_tier` can only
    # apply to Scout-hosted models: a local model is unreachable from the
    # primary session and must always be executed out of band, whatever the
    # tier says.
    cloud_fast = reference_config()
    cloud_fast["tiers"]["fast"]["model"] = "scout/cloud-fast"

    return [
        _case("greeting on a cloud fast tier -> handled inline",
              "hi", {"tier": "fast", "sensitivity": "normal", "should_delegate": False},
              config=cloud_fast),
        _case("greeting on a LOCAL fast tier -> still delegated (session cannot host it)",
              "hi", {"tier": "fast", "sensitivity": "normal",
                     "egress": "on-device", "should_delegate": True}),
        _case("short question -> fast tier",
              "What is 2+2?", {"tier": "fast", "difficulty": "simple"}),
        _case("debugging -> coding role, cloud codex",
              "Debug this Python function that has a bug in the import logic",
              {"role": "coding", "model": "scout/cloud-coder", "should_delegate": True}),
        _case("strategy -> strong reasoning model",
              "Analyze the strategic trade-offs of our go-to-market roadmap",
              {"role": "strategy", "model": "scout/cloud-strong", "tier": "strong"}),
        _case("creative -> creative role model",
              "Write a blog post about AI consciousness for our newsletter",
              {"role": "creative", "model": "scout/cloud-writer"}),
        _case("secret in text -> restricted, on-device, delegated",
              "Rotate this for me: api_key = sk-live-abcd1234efgh5678",
              {"sensitivity": "restricted", "egress": "on-device",
               "model": "local/ollama/qwen3:8b", "should_delegate": True}),
        _case("SSN -> restricted, on-device",
              "The employee record lists 123-45-6789 as the identifier",
              {"sensitivity": "restricted", "egress": "on-device"}),
        _case("Highly Confidential label -> restricted, on-device",
              "Summarize the attached quarterly plan.",
              {"sensitivity": "restricted", "egress": "on-device"},
              label="Highly Confidential"),
        _case("Confidential label -> org tenant",
              "Summarize the attached quarterly plan.",
              {"sensitivity": "confidential", "egress": "org-tenant",
               "model": "org/tenant/gpt-4o"},
              label="Confidential"),
        _case("internal-only marker -> confidential, org tenant",
              "This deck is Microsoft Internal — draft talking points from it.",
              {"sensitivity": "confidential", "egress": "org-tenant"}),
        _case("M365 email provenance -> confidential floor",
              "Summarize the thread and list the open questions.",
              {"sensitivity": "confidential", "egress": "org-tenant"},
              source="email"),
        _case("web provenance stays normal",
              "Summarize the thread and list the open questions.",
              {"sensitivity": "normal"}, source="web"),
        # ── Regression tests for defects found in the Hermes original ──
        _case("REGRESSION: restricted content with no local model is BLOCKED, not downgraded",
              "Here is the password: hunter2ExampleValue",
              {"status": ROUTE_BLOCKED, "model": ""},
              config=cloud_only),
        _case("REGRESSION: confidential content with no in-boundary model is BLOCKED",
              "This document is marked Confidential.",
              {"status": ROUTE_BLOCKED, "model": ""},
              config=cloud_only),
        _case("REGRESSION: no cloud model appears in a restricted fallback chain",
              "api_key = sk-live-zzzz9999",
              {"sensitivity": "restricted", "no_cloud_in_fallbacks": True}),
        _case("REGRESSION: 'contest' does not match the coding cue 'test'",
              "Who won the contest we ran on the latest campaign?",
              {"role": "general"}),
        _case("REGRESSION: 'greatest' does not match the coding cue 'test'",
              "Give me the greatest hits from the newsletter",
              {"role": "general"}),
        _case("policy cap forces on-device for ordinary content",
              "Analyze the trade-offs in this architecture",
              {"egress": "on-device", "model": "local/ollama/phi4-mini"},
              config=locked_down),
        _case("unknown model scheme is rejected, not guessed",
              "Say hello",
              {"status": ROUTE_BLOCKED},
              config=reference_config(
                  policy={"max_egress": "on-device"},
                  sensitivity={"restricted_model": "mystery/provider/model",
                               "confidential_model": "",
                               "restricted_patterns": [], "confidential_patterns": []},
                  tiers={"fast": {"model": "mystery/provider/model"},
                         "balanced": {"model": ""}, "strong": {"model": ""}},
                  roles={},
              )),
    ]


def run_selftest(user_config_path: str | None = None) -> dict:
    default_router = HybridRouter(reference_config())
    cases_out: list[dict] = []
    passed = 0

    for case in _cases():
        router = HybridRouter(case["config"]) if case["config"] else default_router
        decision = router.route(case["text"], label=case["label"], source=case["source"])
        actual = decision.to_dict()

        expect = dict(case["expect"])
        no_cloud = expect.pop("no_cloud_in_fallbacks", False)
        expect.setdefault("status", ROUTE_OK)

        diffs = {}
        for key, want in expect.items():
            got = actual.get(key)
            if got != want:
                diffs[key] = {"expected": want, "actual": got}

        if no_cloud:
            leaked = [
                candidate["ref"]
                for candidate in actual.get("fallback_chain", [])
                if candidate.get("egress") == "cloud-public"
            ]
            if leaked:
                diffs["fallback_chain"] = {
                    "expected": "no cloud-public entries",
                    "actual": leaked,
                }

        ok = not diffs
        passed += ok
        cases_out.append(
            {
                "name": case["name"],
                "passed": ok,
                "expected": expect,
                "actual": {key: actual.get(key) for key in expect},
                "diffs": diffs,
            }
        )

    result = {"passed": passed, "total": len(cases_out), "cases": cases_out}

    if user_config_path is not None:
        try:
            cfg, path = load_config(user_config_path)
            result["user_config"] = {
                "path": str(path),
                "problems": HybridRouter(cfg).validate(),
            }
        except Exception as exc:  # noqa: BLE001
            result["user_config"] = {"error": str(exc)}
    return result
