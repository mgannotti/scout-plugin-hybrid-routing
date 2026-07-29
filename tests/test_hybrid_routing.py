"""Tests for the hybrid contextual inference router.

Includes explicit regression tests for three defects observed in the Hermes
original this plugin is modelled on:

  1. sensitive content falls back to the balanced *cloud* tier when no local
     model is configured (fail-open),
  2. cloud models are appended to the fallback chain of a sensitive decision,
  3. role cues match as bare substrings, so "test" fires on "contest".
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from hybrid_routing import (
    CLOUD_PUBLIC,
    ON_DEVICE,
    ORG_TENANT,
    Backend,
    BackendError,
    ConfigError,
    HybridRouter,
    ModelRefError,
    load_config,
    parse_model_ref,
    permits,
    resolve_egress,
    url_is_loopback,
)
from hybrid_routing.egress import resolve_backend_egress
from hybrid_routing.classify import CONFIDENTIAL, NORMAL, RESTRICTED, normalize_label
from hybrid_routing.mcp_server import handle
from hybrid_routing.router import ROUTE_BLOCKED, ROUTE_OK, ROUTE_UNCONFIGURED
from hybrid_routing.selftest import reference_config, run_selftest


@pytest.fixture
def router():
    return HybridRouter(reference_config())


@pytest.fixture
def cloud_only_router():
    cfg = reference_config()
    cfg["sensitivity"]["restricted_model"] = ""
    cfg["sensitivity"]["confidential_model"] = ""
    cfg["tiers"]["fast"]["model"] = "scout/cloud-fast"
    return HybridRouter(cfg)


# ── Model references and egress ────────────────────────────────────────
class TestModelRef:
    @pytest.mark.parametrize(
        "ref,scheme,backend,model_id,egress",
        [
            ("scout/cloud-strong", "scout", "scout", "cloud-strong", CLOUD_PUBLIC),
            ("local/ollama/qwen3:8b", "local", "ollama", "qwen3:8b", ON_DEVICE),
            ("org/tenant/gpt-4o", "org", "tenant", "gpt-4o", ORG_TENANT),
            ("org/foundry/publisher/model-v2", "org", "foundry", "publisher/model-v2", ORG_TENANT),
        ],
    )
    def test_parses(self, ref, scheme, backend, model_id, egress):
        parsed = parse_model_ref(ref)
        assert (parsed.scheme, parsed.backend, parsed.model_id) == (scheme, backend, model_id)
        assert parsed.egress == egress

    @pytest.mark.parametrize(
        "ref", ["", "   ", "cloud-strong", "mystery/provider/model", "scout/", "local/ollama"]
    )
    def test_rejects_unparseable(self, ref):
        with pytest.raises(ModelRefError):
            parse_model_ref(ref)

    def test_unknown_scheme_is_never_guessed(self):
        """An unrecognized scheme must raise, not default to a trust level."""
        with pytest.raises(ModelRefError, match="unknown scheme"):
            parse_model_ref("openai/gpt-4")

    def test_ceiling_ordering(self):
        assert permits(CLOUD_PUBLIC, ON_DEVICE)
        assert permits(CLOUD_PUBLIC, CLOUD_PUBLIC)
        assert permits(ORG_TENANT, ON_DEVICE)
        assert not permits(ORG_TENANT, CLOUD_PUBLIC)
        assert not permits(ON_DEVICE, ORG_TENANT)
        assert not permits(ON_DEVICE, CLOUD_PUBLIC)


# ── Classification ─────────────────────────────────────────────────────
class TestSensitivity:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("what is the weather", NORMAL),
            ("api_key = sk-live-abcd1234", RESTRICTED),
            ("password: hunter2example", RESTRICTED),
            ("employee ssn 123-45-6789", RESTRICTED),
            ("-----BEGIN RSA PRIVATE KEY-----", RESTRICTED),
            ("this is Highly Confidential material", RESTRICTED),
            ("patient MRN: 88213", RESTRICTED),
            ("marked Confidential, please review", CONFIDENTIAL),
            ("Microsoft Internal roadmap notes", CONFIDENTIAL),
            ("this is an unreleased feature", CONFIDENTIAL),
        ],
    )
    def test_text_patterns(self, router, text, expected):
        level, _ = router.classifier.classify_sensitivity(text)
        assert level == expected

    @pytest.mark.parametrize(
        "label,expected",
        [
            (None, NORMAL),
            ("Public", NORMAL),
            ("General", NORMAL),
            ("Confidential", CONFIDENTIAL),
            ("Internal Only", CONFIDENTIAL),
            ("Highly Confidential", RESTRICTED),
            ("Highly Confidential \\ Any Employee", RESTRICTED),
            ("Restricted", RESTRICTED),
        ],
    )
    def test_mip_labels(self, label, expected):
        assert normalize_label(label) == expected

    def test_highly_confidential_not_swallowed_by_confidential(self):
        """The 'confidential' substring must not downgrade 'Highly Confidential'."""
        assert normalize_label("Highly Confidential") == RESTRICTED

    @pytest.mark.parametrize("source", ["email", "teams", "sharepoint", "calendar", "transcript"])
    def test_m365_provenance_floors_at_confidential(self, router, source):
        level, _ = router.classifier.classify_sensitivity("summarize this", source=source)
        assert level == CONFIDENTIAL

    def test_web_provenance_stays_normal(self, router):
        level, _ = router.classifier.classify_sensitivity("summarize this", source="web")
        assert level == NORMAL

    def test_signals_do_not_echo_the_secret(self, router):
        secret = "sk-live-SUPERSECRETVALUE1234567890"
        _, signals = router.classifier.classify_sensitivity(f"api_key = {secret}")
        assert signals
        assert secret not in " ".join(signals)

    def test_most_restrictive_signal_wins(self, router):
        """A benign label must not lower a level the text itself established."""
        level, _ = router.classifier.classify_sensitivity(
            "api_key = sk-live-abcd1234", label="Public", source="web"
        )
        assert level == RESTRICTED


class TestRole:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Debug this function and fix the stack trace", "coding"),
            ("Compare the arxiv paper against prior work", "research"),
            ("Write a blog post for the newsletter", "creative"),
            ("Analyze the go-to-market strategy trade-off", "strategy"),
            ("What time is the meeting", "general"),
        ],
    )
    def test_role_detection(self, router, text, expected):
        assert router.classifier.classify_role(text) == expected

    @pytest.mark.parametrize(
        "text", ["Who won the contest?", "the greatest hits", "our latest results", "protest march"]
    )
    def test_regression_substring_cue_false_positives(self, router, text):
        """Hermes bug 3: the cue 'test' matched inside contest/greatest/latest."""
        assert router.classifier.classify_role(text) == "general"


class TestDifficulty:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("hi", "simple"),
            ("thanks", "simple"),
            ("What is 2+2?", "simple"),
            ("Summarize the notes from the sync", "standard"),
            ("Analyze the trade-offs in this design", "hard"),
            ("```python\nprint(1)\n```", "hard"),
            ("x " * 100, "hard"),
        ],
    )
    def test_difficulty(self, router, text, expected):
        assert router.classifier.classify_difficulty(text) == expected


# ── Routing and egress enforcement ─────────────────────────────────────
class TestRouting:
    def test_normal_content_uses_cloud(self, router):
        decision = router.route("Analyze the strategic roadmap trade-offs")
        assert decision.status == ROUTE_OK
        assert decision.egress == CLOUD_PUBLIC

    def test_restricted_routes_on_device(self, router):
        decision = router.route("api_key = sk-live-abcd1234")
        assert decision.status == ROUTE_OK
        assert decision.egress == ON_DEVICE
        assert decision.model == "local/ollama/qwen3:8b"

    def test_confidential_routes_to_tenant(self, router):
        decision = router.route("This deck is Confidential — summarize it")
        assert decision.status == ROUTE_OK
        assert decision.egress == ORG_TENANT

    def test_mip_label_alone_drives_routing(self, router):
        decision = router.route("Summarize the attached plan.", label="Highly Confidential")
        assert decision.egress == ON_DEVICE

    def test_regression_fail_closed_not_downgraded(self, cloud_only_router):
        """Hermes bug 1: sensitive content fell back to the balanced cloud tier."""
        decision = cloud_only_router.route("password: hunter2example")
        assert decision.status == ROUTE_BLOCKED
        assert decision.model == ""
        assert decision.egress != CLOUD_PUBLIC
        assert "BLOCKED" in decision.reason

    def test_regression_confidential_also_fails_closed(self, cloud_only_router):
        decision = cloud_only_router.route("This document is Confidential.")
        assert decision.status == ROUTE_BLOCKED

    def test_regression_no_cloud_in_sensitive_fallbacks(self, router):
        """Hermes bug 2: all tier models were appended to a sensitive chain."""
        decision = router.route("api_key = sk-live-abcd1234")
        egresses = {c["egress"] for c in decision.fallbacks}
        assert CLOUD_PUBLIC not in egresses
        assert ORG_TENANT not in egresses

    def test_blocked_decision_names_what_was_excluded(self, cloud_only_router):
        decision = cloud_only_router.route("password: hunter2example")
        assert decision.rejected
        assert all(r["ref"] for r in decision.rejected)

    def test_policy_cap_applies_to_normal_content(self):
        cfg = reference_config()
        cfg["policy"]["max_egress"] = ON_DEVICE
        decision = HybridRouter(cfg).route("Analyze this architecture")
        assert decision.egress == ON_DEVICE

    def test_unconfigured_reports_rather_than_guessing(self):
        cfg = reference_config()
        cfg["tiers"] = {t: {"model": ""} for t in ("fast", "balanced", "strong")}
        cfg["roles"] = {}
        cfg["sensitivity"]["restricted_model"] = ""
        cfg["sensitivity"]["confidential_model"] = ""
        decision = HybridRouter(cfg).route("hello")
        assert decision.status == ROUTE_UNCONFIGURED
        assert decision.model == ""


class TestDelegation:
    def test_cloud_fast_tier_handled_inline(self):
        cfg = reference_config()
        cfg["tiers"]["fast"]["model"] = "scout/cloud-fast"
        assert HybridRouter(cfg).route("hi").should_delegate is False

    def test_local_model_always_delegates(self, router):
        """Scout's session cannot host a local model, whatever the tier says."""
        decision = router.route("hi")
        assert decision.egress == ON_DEVICE
        assert decision.should_delegate is True

    def test_matching_primary_is_inline(self):
        cfg = reference_config()
        cfg["delegation"]["primary_model"] = "scout/cloud-balanced"
        decision = HybridRouter(cfg).route("Summarize the notes from the sync")
        assert decision.model == "scout/cloud-balanced"
        assert decision.should_delegate is False

    def test_sensitive_always_delegates(self, router):
        assert router.route("api_key = sk-live-abcd1234").should_delegate is True

    def test_execution_block_is_actionable(self, router):
        cloud = router.route("Debug this function and fix the stack trace")
        assert cloud.execution["call"]["tool"] == "task"
        assert cloud.execution["call"]["model"] == "cloud-coder"

        local = router.route("api_key = sk-live-abcd1234")
        assert local.execution["mode"] == "openai-http"
        assert local.execution["backend"] == "ollama"


class TestValidation:
    def test_flags_cloud_model_for_restricted_slot(self):
        cfg = reference_config()
        cfg["sensitivity"]["restricted_model"] = "scout/cloud-balanced"
        problems = HybridRouter(cfg).validate()
        assert any("restricted_model" in p for p in problems)

    def test_flags_cloud_model_for_confidential_slot(self):
        cfg = reference_config()
        cfg["sensitivity"]["confidential_model"] = "scout/cloud-balanced"
        assert any("confidential_model" in p for p in HybridRouter(cfg).validate())

    def test_reference_config_is_clean(self, router):
        assert router.validate() == []

    def test_local_model_allowed_for_confidential_slot(self):
        """on-device is inside org-tenant, so it must be accepted."""
        cfg = reference_config()
        cfg["sensitivity"]["confidential_model"] = "local/ollama/qwen3"
        assert HybridRouter(cfg).validate() == []


# ── Second-pass regression tests ───────────────────────────────────────
# Every bug below was live while all 86 first-pass tests passed. The suite
# was written by the same author as the code and inherited its blind spots:
# it only exercised references whose scheme agreed with their backend, and
# only used secrets long enough to survive truncation.
class TestEgressReconciliation:
    """A reference's scheme is a claim; the backend decides where bytes go."""

    def test_local_scheme_with_cloud_backend_is_not_trusted(self):
        cfg = reference_config()
        cfg["backends"]["sneaky"] = {
            "base_url": "https://api.openai.com/v1",
            "egress": "cloud-public",
        }
        cfg["sensitivity"]["restricted_model"] = "local/sneaky/gpt-4o"
        router = HybridRouter(cfg)
        assert any("sneaky" in p for p in router.validate())
        assert router.route("api_key = sk-live-abcd1234").model != "local/sneaky/gpt-4o"

    def test_local_backend_on_a_remote_host_cannot_be_on_device(self):
        cfg = reference_config()
        cfg["backends"]["ollama"]["base_url"] = "https://evil.example.com/v1"
        router = HybridRouter(cfg)
        assert any("loopback" in p for p in router.validate())
        assert router.route("api_key = sk-live-abcd1234").status == ROUTE_BLOCKED

    def test_undefined_backend_is_treated_as_untrusted(self):
        cfg = reference_config()
        cfg["sensitivity"]["restricted_model"] = "local/nonexistent/qwen3"
        router = HybridRouter(cfg)
        assert any("not defined" in p for p in router.validate())
        assert router.route("api_key = sk-live-abcd1234").model != "local/nonexistent/qwen3"

    def test_backend_with_unparseable_egress_is_flagged(self):
        cfg = reference_config()
        cfg["backends"]["ollama"]["egress"] = "on_device"  # underscore, not hyphen
        assert any("on_device" in p for p in HybridRouter(cfg).validate())

    def test_backend_without_base_url_is_flagged(self):
        cfg = reference_config()
        cfg["backends"]["ollama"]["base_url"] = ""
        assert any("base_url" in p for p in HybridRouter(cfg).validate())

    def test_resolve_egress_takes_the_least_trusted_signal(self):
        backends = {"b": {"base_url": "https://remote.example.com/v1", "egress": "org-tenant"}}
        resolved = resolve_egress(parse_model_ref("local/b/m"), backends)
        assert resolved.egress == ORG_TENANT
        assert resolved.conflicts

    def test_scout_refs_need_no_backend(self):
        assert resolve_egress(parse_model_ref("scout/cloud-balanced"), {}).ok

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("http://localhost:11434/v1", True),
            ("http://127.0.0.1:1234/v1", True),
            ("http://[::1]:8080/v1", True),
            ("https://api.openai.com/v1", False),
            ("https://localhost.evil.com/v1", False),
            ("", False),
        ],
    )
    def test_loopback_detection(self, url, expected):
        assert url_is_loopback(url) is expected

    def test_decision_reports_resolved_not_claimed_egress(self):
        cfg = reference_config()
        cfg["backends"]["ollama"]["base_url"] = "https://remote.example.com/v1"
        cfg["sensitivity"]["confidential_model"] = ""
        decision = HybridRouter(cfg).route("This deck is Confidential - summarize it")
        assert decision.status == ROUTE_OK
        assert decision.egress == ORG_TENANT  # not the claimed on-device


class TestSignalRedaction:
    """Audit signals must never reproduce the value that triggered them."""

    @pytest.mark.parametrize(
        "text,secret",
        [
            ("employee ssn 123-45-6789", "123-45-6789"),
            ("MRN: 4471", "4471"),
            ("card 4111111111111111", "4111111111111111"),
            ("api_key = sk-live-SECRET", "sk-live-SECRET"),
            ("password: hunter2", "hunter2"),
        ],
    )
    def test_short_secrets_are_not_echoed(self, router, text, secret):
        """v1 truncated to 12 chars, so an 11-char SSN survived intact."""
        _, signals = router.classifier.classify_sensitivity(text)
        assert signals
        joined = " ".join(signals)
        assert secret not in joined, f"signal leaked {secret!r}: {joined}"

    def test_shape_is_still_auditable(self, router):
        _, signals = router.classifier.classify_sensitivity("ssn 123-45-6789")
        assert "###.##.####" in " ".join(signals)

    def test_no_digits_survive_redaction(self, router):
        _, signals = router.classifier.classify_sensitivity("ssn 987-65-4321")
        shape = " ".join(signals).split("shape")[-1]
        assert not any(char.isdigit() for char in shape)


class TestPolicyCap:
    """A cap that cannot be parsed must fail closed, not silently vanish."""

    @pytest.mark.parametrize("value", ["on-device", "On-Device", "  ON-DEVICE  "])
    def test_casing_and_whitespace_are_forgiven(self, value):
        cfg = reference_config()
        cfg["policy"]["max_egress"] = value
        assert HybridRouter(cfg).route("Analyze the trade-offs here").egress == ON_DEVICE

    @pytest.mark.parametrize("value", ["ondevice", "local", "onprem", "nope"])
    def test_unparseable_cap_fails_closed_and_is_reported(self, value):
        """v1 silently ignored anything it did not recognize."""
        cfg = reference_config()
        cfg["policy"]["max_egress"] = value
        router = HybridRouter(cfg)
        assert router.route("Analyze the trade-offs here").egress == ON_DEVICE
        assert any("max_egress" in p for p in router.validate())

    def test_empty_cap_is_not_a_problem(self):
        cfg = reference_config()
        cfg["policy"]["max_egress"] = ""
        router = HybridRouter(cfg)
        assert not any("max_egress" in p for p in router.validate())
        assert router.route("Analyze the trade-offs here").egress == CLOUD_PUBLIC


# ── Third-pass regression tests ────────────────────────────────────────
# Found by an independent reviewer after the second pass. The theme: routing
# was sound, but nothing enforced it at the point of transmission, and several
# ways of *silently removing* a control went unreported.
class TestInferenceAuthorization:
    """A routing decision nothing checks at send time is advice, not a control."""

    def test_restricted_content_refused_on_org_backend(self, router):
        auth = router.authorize_inference("tenant", "api_key = sk-live-abcd1234")
        assert auth["allowed"] is False
        assert "REFUSED" in auth["reason"]

    def test_restricted_content_allowed_on_loopback_backend(self, router):
        assert router.authorize_inference("ollama", "api_key = sk-live-abcd1234")["allowed"]

    def test_confidential_content_refused_on_undefined_backend(self, router):
        assert router.authorize_inference("nope", "This is Confidential")["allowed"] is False

    def test_policy_cap_is_enforced_at_send_time(self):
        """v1.1 checked the cap when routing but not when inferring."""
        cfg = reference_config()
        cfg["policy"]["max_egress"] = ON_DEVICE
        auth = HybridRouter(cfg).authorize_inference("tenant", "just an ordinary question")
        assert auth["allowed"] is False

    def test_label_is_honoured_at_send_time(self, router):
        auth = router.authorize_inference(
            "tenant", "Summarize the plan", label="Highly Confidential"
        )
        assert auth["allowed"] is False

    def test_mcp_route_infer_refuses_before_calling_the_backend(self, monkeypatch):
        """The refusal must happen without any network call being attempted."""
        import hybrid_routing.mcp_server as server

        monkeypatch.setattr(server, "_load", lambda: reference_config())

        def explode(*args, **kwargs):
            raise AssertionError("backend was contacted despite a refusal")

        monkeypatch.setattr(server, "get_backend", explode)
        result = server._tool_route_infer(
            {"backend": "tenant", "model": "gpt-4o", "prompt": "api_key = sk-live-abcd"}
        )
        assert "error" in result
        assert result["authorization"]["allowed"] is False

    def test_system_prompt_is_classified_too(self, router):
        """A secret hidden in the system prompt must not slip past."""
        auth = router.authorize_inference("tenant", "You may use api_key = sk-live-abcd1234")
        assert auth["allowed"] is False


class TestProxyAndTransport:
    def test_loopback_requests_bypass_the_proxy(self):
        """urlopen honours $HTTP_PROXY; localhost is not excluded by default."""
        backend = Backend(name="o", base_url="http://localhost:11434/v1")
        proxies = [
            handler.proxies
            for handler in backend._opener().handlers
            if isinstance(handler, urllib.request.ProxyHandler)
        ]
        assert all(p == {} for p in proxies)

    def test_loopback_requests_refuse_redirects(self):
        backend = Backend(name="o", base_url="http://localhost:11434/v1")
        names = {type(h).__name__ for h in backend._opener().handlers}
        assert "_NoRedirect" in names
        assert "HTTPRedirectHandler" not in names

    def test_remote_backends_keep_normal_redirect_handling(self):
        backend = Backend(name="r", base_url="https://api.example.com/v1")
        names = {type(h).__name__ for h in backend._opener().handlers}
        assert "_NoRedirect" not in names

    def test_cleartext_remote_backend_is_flagged(self):
        cfg = reference_config()
        cfg["backends"]["tenant"]["base_url"] = "http://10.0.0.5:8000/v1"
        assert any("cleartext" in p for p in HybridRouter(cfg).validate())

    def test_cleartext_remote_backend_loses_trust(self):
        resolved = resolve_backend_egress(
            "t", {"t": {"base_url": "http://10.0.0.5:8000/v1", "egress": "org-tenant"}}
        )
        assert resolved.egress == CLOUD_PUBLIC

    def test_loopback_http_is_fine(self):
        resolved = resolve_backend_egress(
            "o", {"o": {"base_url": "http://localhost:11434/v1", "egress": "on-device"}}
        )
        assert resolved.egress == ON_DEVICE
        assert resolved.ok

    def test_backend_defaults_to_least_trusted_not_on_device(self):
        backend = Backend.from_config("x", {"base_url": "https://api.example.com/v1"})
        assert backend.egress == CLOUD_PUBLIC

    def test_non_numeric_timeout_is_an_error_not_a_crash(self):
        with pytest.raises(BackendError, match="timeout"):
            Backend.from_config("x", {"base_url": "http://localhost:1/v1", "timeout": "abc"})


class TestSilentControlLoss:
    """Ways a security control could vanish without anyone being told."""

    def test_missing_backend_egress_is_not_trusted(self):
        """v1.1 let a backend with no `egress:` key inherit the scheme's claim."""
        resolved = resolve_egress(
            parse_model_ref("org/t/m"), {"t": {"base_url": "https://api.openai.com/v1"}}
        )
        assert resolved.egress == CLOUD_PUBLIC
        assert resolved.conflicts

    def test_invalid_regex_is_reported_not_dropped(self):
        """v1.1 swallowed re.error, silently deleting the protection rule."""
        cfg = reference_config()
        cfg["sensitivity"]["restricted_patterns"] = [r"(?i)\bpassword\s*[:=]\s*\S+("]
        problems = HybridRouter(cfg).validate()
        assert any("not a valid regex" in p for p in problems)

    def test_invalid_regex_does_not_break_other_patterns(self):
        cfg = reference_config()
        cfg["sensitivity"]["restricted_patterns"] = [r"bad(", r"\b\d{3}-\d{2}-\d{4}\b"]
        level, _ = HybridRouter(cfg).classifier.classify_sensitivity("ssn 123-45-6789")
        assert level == RESTRICTED

    @pytest.mark.parametrize("typo", ["sensitivty", "policyy", "backend"])
    def test_unknown_top_level_key_is_reported(self, typo):
        cfg = reference_config()
        cfg[typo] = {"whatever": 1}
        assert any(typo in p for p in HybridRouter(cfg).validate())

    def test_unknown_nested_key_is_reported(self):
        cfg = reference_config()
        cfg["policy"]["max_egres"] = "on-device"  # missing 's'
        assert any("max_egres" in p for p in HybridRouter(cfg).validate())

    def test_typoed_sensitivity_section_is_loud(self):
        """The worst case: every detection pattern silently disappears."""
        cfg = reference_config()
        cfg["sensitivty"] = cfg.pop("sensitivity")
        router = HybridRouter(cfg)
        level, _ = router.classifier.classify_sensitivity("password: hunter2example")
        assert level == NORMAL  # detection really is gone...
        assert any("sensitivty" in p for p in router.validate())  # ...but it is reported


class TestSymbolSecrets:
    def test_symbol_only_secret_is_not_echoed(self):
        """v1.1 masked alphanumerics but kept punctuation verbatim."""
        cfg = reference_config()
        cfg["sensitivity"]["restricted_patterns"] = [r"(?i)secret\s*=\s*\S+"]
        _, signals = HybridRouter(cfg).classifier.classify_sensitivity("secret=+-*/")
        assert "+-*/" not in " ".join(signals)

    def test_shape_still_distinguishes_an_ssn(self, router):
        _, signals = router.classifier.classify_sensitivity("ssn 123-45-6789")
        assert "###.##.####" in " ".join(signals)


class TestProvenanceMode:
    """v1.3 — the M365 provenance floor was hardcoded with no way to tune it."""

    def _route(self, source="email", text="Summarize this thread", label=None, **prov):
        cfg = reference_config()
        if prov:
            cfg["provenance"] = prov
        return HybridRouter(cfg), HybridRouter(cfg).route(text, label=label, source=source)

    def test_default_is_floor_preserving_v12_behaviour(self):
        """Shipping a looser default would silently remove a control on upgrade."""
        cfg = reference_config()
        cfg.pop("provenance", None)
        assert HybridRouter(cfg).route("Summarize this", source="email").sensitivity == CONFIDENTIAL

    def test_floor_raises_the_level(self):
        _, d = self._route(mode="floor")
        assert d.sensitivity == CONFIDENTIAL

    def test_advisory_records_but_does_not_raise(self):
        _, d = self._route(mode="advisory")
        assert d.sensitivity == NORMAL
        assert any("advisory" in s for s in d.signals)

    def test_off_is_silent(self):
        _, d = self._route(mode="off")
        assert d.sensitivity == NORMAL
        assert not any("provenance" in s.lower() for s in d.signals)

    def test_advisory_does_not_weaken_label_detection(self):
        _, d = self._route(mode="advisory", label="Highly Confidential")
        assert d.sensitivity == RESTRICTED

    def test_advisory_does_not_weaken_pattern_detection(self):
        _, d = self._route(text="api_key = sk-live-abcd1234", mode="advisory")
        assert d.sensitivity == RESTRICTED

    def test_per_source_override_can_floor_under_advisory(self):
        cfg = reference_config()
        cfg["provenance"] = {"mode": "advisory", "sources": {"sharepoint": "confidential"}}
        router = HybridRouter(cfg)
        assert router.route("Summarize", source="sharepoint").sensitivity == CONFIDENTIAL
        assert router.route("Summarize", source="email").sensitivity == NORMAL

    def test_per_source_override_can_exempt_under_floor(self):
        cfg = reference_config()
        cfg["provenance"] = {"mode": "floor", "sources": {"email": "normal"}}
        router = HybridRouter(cfg)
        assert router.route("Summarize", source="email").sensitivity == NORMAL
        assert router.route("Summarize", source="teams").sensitivity == CONFIDENTIAL

    def test_per_source_override_can_raise_to_restricted(self):
        cfg = reference_config()
        cfg["provenance"] = {"mode": "advisory", "sources": {"transcript": "restricted"}}
        d = HybridRouter(cfg).route("Summarize", source="transcript")
        assert d.sensitivity == RESTRICTED
        assert d.egress == ON_DEVICE

    @pytest.mark.parametrize("value", ["Advisory", "  ADVISORY  ", "advisory"])
    def test_mode_casing_is_forgiven(self, value):
        _, d = self._route(mode=value)
        assert d.sensitivity == NORMAL

    @pytest.mark.parametrize("value", ["adivsory", "none", "disabled", "no"])
    def test_unreadable_mode_fails_closed_and_is_reported(self, value):
        router, d = self._route(mode=value)
        assert d.sensitivity == CONFIDENTIAL
        assert any("provenance.mode" in p for p in router.validate())

    def test_unreadable_default_level_fails_closed_and_is_reported(self):
        router, d = self._route(mode="floor", default_level="konfidential")
        assert d.sensitivity == CONFIDENTIAL
        assert any("default_level" in p for p in router.validate())

    def test_unreadable_source_override_fails_closed_and_is_reported(self):
        cfg = reference_config()
        cfg["provenance"] = {"mode": "advisory", "sources": {"email": "kinda-secret"}}
        router = HybridRouter(cfg)
        assert router.route("Summarize", source="email").sensitivity == CONFIDENTIAL
        assert any("sources" in p for p in router.validate())

    def test_non_m365_source_is_unaffected_by_floor(self):
        _, d = self._route(source="web", mode="floor")
        assert d.sensitivity == NORMAL

    def test_provenance_is_a_known_config_section(self):
        cfg = reference_config()
        cfg["provenance"] = {"mode": "advisory"}
        assert not any("provenance" in p for p in HybridRouter(cfg).validate())

    def test_typoed_provenance_key_is_reported(self):
        cfg = reference_config()
        cfg["provenance"] = {"moode": "advisory"}
        assert any("moode" in p for p in HybridRouter(cfg).validate())


class TestMalformedInput:
    def test_invalid_yaml_is_a_config_error(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("tiers: [unclosed\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="valid YAML"):
            load_config(str(path))

    def test_non_mapping_config_is_a_config_error(self, tmp_path):
        path = tmp_path / "list.yaml"
        path.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="not a mapping"):
            load_config(str(path))

    def test_missing_config_is_a_config_error(self):
        with pytest.raises(ConfigError):
            load_config(str(Path("no", "such", "file.yaml")))


# ── Backends ───────────────────────────────────────────────────────────
class _MockOpenAIHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: A002 - silence test server logging
        pass

    def _send(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/models"):
            self._send({"data": [{"id": "mock-local-7b"}]})
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        prompt = payload["messages"][-1]["content"]
        self._send(
            {
                "model": payload["model"],
                "choices": [{"message": {"role": "assistant", "content": f"echo: {prompt}"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 5},
            }
        )


@pytest.fixture(scope="module")
def mock_server():
    server = HTTPServer(("127.0.0.1", 0), _MockOpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


class TestBackend:
    def test_lists_models(self, mock_server):
        backend = Backend(name="mock", base_url=mock_server)
        assert backend.list_models() == ["mock-local-7b"]

    def test_chat_round_trip(self, mock_server):
        backend = Backend(name="mock", base_url=mock_server, egress=ON_DEVICE)
        result = backend.chat(model="mock-local-7b", prompt="ping")
        assert result["content"] == "echo: ping"
        assert result["egress"] == ON_DEVICE

    def test_health_reports_reachable(self, mock_server):
        assert Backend(name="mock", base_url=mock_server).health()["reachable"] is True

    def test_health_reports_unreachable(self):
        health = Backend(name="dead", base_url="http://127.0.0.1:9/v1").health()
        assert health["reachable"] is False
        assert health["error"]

    def test_missing_api_key_is_an_error_not_an_anonymous_call(self, mock_server, monkeypatch):
        monkeypatch.delenv("HR_TEST_KEY", raising=False)
        backend = Backend(name="mock", base_url=mock_server, api_key_env="HR_TEST_KEY")
        health = backend.health()
        assert health["reachable"] is False
        assert "HR_TEST_KEY" in health["error"]


# ── MCP surface ────────────────────────────────────────────────────────
class TestMCP:
    def test_initialize(self):
        response = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert response["result"]["serverInfo"]["name"] == "hybrid-routing"

    def test_tools_listed(self):
        response = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in response["result"]["tools"]}
        assert names == {
            "route_classify",
            "route_status",
            "route_test",
            "route_probe",
            "route_infer",
        }

    def test_every_tool_has_a_handler(self):
        from hybrid_routing.mcp_server import HANDLERS, TOOLS

        assert {t["name"] for t in TOOLS} == set(HANDLERS)

    def test_notification_returns_nothing(self):
        assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    def test_unknown_tool_errors(self):
        response = handle(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope"}}
        )
        assert "error" in response

    def test_classify_through_mcp(self):
        response = handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "route_classify", "arguments": {"text": "hi"}},
            }
        )
        payload = json.loads(response["result"]["content"][0]["text"])
        assert "status" in payload


# ── Self-test ──────────────────────────────────────────────────────────
def test_selftest_fully_passes():
    results = run_selftest()
    failures = [c["name"] for c in results["cases"] if not c["passed"]]
    assert not failures, f"self-test failures: {failures}"
    assert results["passed"] == results["total"]
