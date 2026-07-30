# Attribution

This project is a derivative work of
**[hermes-plugin-hybrid-routing](https://github.com/smfworks/hermes-plugin-hybrid-routing)**
— MIT licensed, Copyright (c) 2026 SMF Works, authored by Aiona Edge.

That plugin established the idea this one is built on: classify a task on
sensitivity, role and difficulty, then route it to the model that fits, using
delegation so the primary session model stays fixed and prompt caching
survives. The design credit is theirs.

## What originates upstream

Carried over substantially unchanged:

- **The three-signal model** — sensitivity, role, difficulty — and the
  precedence between them (sensitivity first, then role, then difficulty tier).
- **The difficulty heuristics** in `data/routing_config.yaml`: the `hard_cues`
  and `simple_cues` lists and the `hard_if_code_block` / `hard_if_long_input` /
  `hard_if_many_words` / `hard_if_many_lines` / `simple_if_short_words`
  thresholds are taken verbatim from the upstream config.
- **Two sensitivity patterns** — the US SSN and payment-card regexes. The
  payment-card regex has since been corrected here (see "Fourth-pass findings"
  in the README): the upstream `{13,16}` form misses 17–19 digit PANs entirely
  and has no Luhn check. The defect was still present upstream at
  `hybrid_contextual_routing/data/routing_config.yaml:131` as of commit
  `88198cf`.
- **The config schema shape** — `tiers` / `roles` / `sensitivity` / `difficulty`
  / `delegation` sections, tier names (`fast` / `balanced` / `strong`), and the
  blank-by-default policy so nothing routes until you configure it.
- **The delegation model** — `primary_model`, `skip_for_tier`,
  `skip_if_same_as_primary`.
- **The tool surface** — `route_classify`, `route_status`, `route_test`.

## What is new here

- **Egress classes** (`on-device` / `org-tenant` / `cloud-public`) as a first-
  class property of every model reference, with routing expressed as a ceiling
  and enforced on both selection and fallback.
- **Fail-closed blocking** — no model inside the boundary means refuse, never
  downgrade.
- **Graded sensitivity** — `normal` / `confidential` / `restricted` rather than
  binary, so tenant-hosted inference is expressible.
- **Microsoft Information Protection label** and **M365 provenance** signals.
- **Backend execution** — an OpenAI-compatible client for Ollama, LM Studio,
  Foundry Local, llama.cpp, vLLM and Azure AI Foundry, since Scout's `task`
  tool hosts only Scout's own cloud models. Upstream delegates execution to
  Hermes' provider layer.
- **Send-time authorization** — `authorize_inference()`, so a routing decision
  is enforced at transmission rather than being advisory.
- **Config validation** — egress reconciliation, loopback and HTTPS checks,
  unknown-key detection, regex compile reporting.
- **MCP stdio server**, plus `route_probe` and `route_infer`.

## Divergences from upstream behaviour

While porting, four defects were found in the upstream engine and are
deliberately not reproduced here. **All four were reported to the upstream
maintainers, who are actively addressing them.** Line references are pinned to
commit `88198cf` (the revision reviewed) and are recorded here to explain why
this port's behaviour differs — not as a standing assessment of the upstream
project, which has almost certainly moved past them by the time you read this.

1. Sensitive content with no local model configured routes to the balanced
   **cloud** tier (`router.py:253-255`). Here it blocks.
2. All tier models are appended to the fallback chain after the sensitivity
   branch (`router.py:270-273`), placing cloud models in a sensitive chain.
   Here fallback chains are egress-filtered.
3. Role cues match as bare substrings (`router.py:137-139`), so `"test"` fires
   on `"contest"`. Here cues match on word boundaries.
4. `simple_cues` is compiled without `re.IGNORECASE` (`router.py:133`), so
   capitalized greetings miss the fast tier. Here all cue sets are
   case-insensitive.

For current upstream behaviour, consult the upstream repository directly rather
than this list.

The bugs found in *this* port during its own review — thirteen of them, five
from self-review and eight from an independent reviewer — are documented in the
README under "Second-pass" and "Third-pass findings". Glass houses.
