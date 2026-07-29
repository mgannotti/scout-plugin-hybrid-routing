# Hybrid Contextual Inference — Microsoft Scout Plugin

Route each task to the right model across a hybrid stack — Scout's hosted cloud
models, on-device runtimes, and org-hosted endpoints — and keep sensitive
content inside the trust boundary it belongs in.

A Scout port of [`smfworks/hermes-plugin-hybrid-routing`][hermes], rebuilt for
Scout's extension model and hardened around the thing that actually makes
hybrid inference worth doing: **provable egress control**.

The three-signal design, the difficulty heuristics and the config schema
originate upstream (MIT, © 2026 SMF Works). See [ATTRIBUTION.md](ATTRIBUTION.md)
for exactly what was carried over and what is new here.

[hermes]: https://github.com/smfworks/hermes-plugin-hybrid-routing

---

## What it does

Classifies every task on three signals, then picks a model:

| Signal | Values | Drives |
|---|---|---|
| **sensitivity** | `normal` / `confidential` / `restricted` | which trust boundary the content may cross |
| **role** | coding, research, creative, strategy, vision, general | model specialization |
| **difficulty** | simple / standard / hard | fast / balanced / strong tier |

The primary session model never changes, so prompt caching survives. Tasks that
need a different model are delegated to a Scout subagent, or executed against a
local / org-hosted endpoint.

## Egress classes

This is the core concept, and the main departure from the Hermes original.
There, "local" is a naming convention inside an opaque `provider/model` string.
Here every model reference declares where inference physically happens:

| Reference | Egress class | Executed via |
|---|---|---|
| `scout/cloud-strong` | `cloud-public` | Scout's `task` tool |
| `org/tenant/gpt-4o` | `org-tenant` | OpenAI-compatible HTTP |
| `local/ollama/qwen3:8b` | `on-device` | OpenAI-compatible HTTP |

Sensitivity sets a **ceiling**, and both model selection and the fallback chain
are filtered by it:

```
normal        →  cloud-public permitted
confidential  →  org-tenant or tighter
restricted    →  on-device only
```

**If no configured model satisfies the ceiling, the router blocks.** It never
degrades to a less-trusted model.

## Differences from the Hermes original

Three defects were found by running the upstream engine before porting. Each
has a named regression test in `tests/test_hybrid_routing.py`.

| # | Severity | Bug | Fix |
|---|---|---|---|
| 1 | **critical** | Sensitive content with no local model configured falls through to the **balanced cloud tier** (`router.py:253-255`), and the hardcoded default `ollama-cloud/glm-5.2` is a cloud endpoint. The feature fails open. | Fails **closed** — returns `status: blocked` with no model. |
| 2 | **high** | All tier models are appended to the fallback chain *after* the sensitivity branch (`router.py:270-273`), so a sensitive decision carries cloud models as fallbacks. | Fallback chains are egress-filtered; a restricted decision contains only on-device candidates. |
| 3 | **medium** | Role cues match as bare substrings, so the cue `test` fires on "contest", "greatest", "latest" and misroutes ordinary prose to the coding model. | Cues match on word boundaries. |
| 4 | **medium** | `simple_cues` is the only pattern set compiled without `re.IGNORECASE` (`router.py:133`), so capitalized greetings miss the fast tier and land on the paid balanced tier. | All cue sets are case-insensitive. |

Line references are pinned to upstream commit `88198cf`. All four were reported
upstream and are being addressed — see [ATTRIBUTION.md](ATTRIBUTION.md).

Plus: the upstream `run_tests()` evaluates against whichever config is loaded,
so its expectations depend on the user's own model choices — with a plausible
hybrid config it self-reports 6/9. Here the self-test pins its own reference
config, so a failure always means the engine changed.

## Second-pass findings (v1.1)

A self-review after v1.0.0 found five more bugs — in *this* code, not upstream.
All 86 v1.0.0 tests passed while every one of them was live, because the tests
were written by the author of the code and inherited its blind spots.

| # | Severity | Bug | Fix |
|---|---|---|---|
| 1 | **critical** | A reference's egress came *only* from its scheme prefix, while the actual network destination came from the backend's `base_url`. The two were never reconciled, so `local/x/model` pointing at a backend declared `cloud-public` was routed as on-device and `validate()` returned clean. Restricted content would have been POSTed to a public endpoint. | `resolve_egress()` reconciles scheme, backend-declared egress and URL, taking the **least trusted** of the three |
| 2 | **critical** | A `local/` backend whose `base_url` was a remote host was still treated as on-device. Nothing checked that "local" meant localhost. | an on-device claim now requires a loopback address, else it degrades to `org-tenant` |
| 3 | **high** | Audit signals truncated a match to 12 characters — but the SSN pattern matches exactly 11, so SSNs and short MRNs were echoed **verbatim** into the decision record. The v1 test used a long secret and passed. | signals now report the match's *shape* (`###-##-####`), masking every alphanumeric |
| 4 | **medium** | `policy.max_egress` was compared literally, so `On-Device`, `ondevice` or any typo silently disabled the cap — a fail-open on the one setting whose entire job is locking things down. | normalized case/whitespace; unrecognized values fail **closed** to on-device and are reported by `validate()` |
| 5 | **medium** | A model reference naming an undefined backend validated clean and routed `ok`, failing only at execution time. | undefined backends resolve to `cloud-public` and are flagged |

The lesson generalizes: bug 1 is the *same class* of failure as the upstream
fail-open — a trust decision made from a value that does not actually control
where the bytes go. Fixing someone else's version of a bug is no protection
against writing your own.

Each fix has a regression test, verified by mutation testing: reverting
`resolve_egress` to the v1 behaviour fails 5 tests, and reverting `_redact`
fails the short-secret cases.

## Third-pass findings (v1.2)

An independent reviewer found eight more. The theme this time: routing itself
was sound, but nothing enforced it at the point of transmission, and several
ways of *silently deleting* a control went unreported.

| # | Severity | Bug | Fix |
|---|---|---|---|
| 1 | **high** | `route_infer` accepted any backend, model and prompt with **no classification at all**. A caller could hand a restricted prompt straight to a cloud-configured backend and never consult the router — making the entire egress model decorative. | `authorize_inference()` re-classifies at send time and refuses when the backend's resolved egress exceeds the ceiling; enforced in both the CLI and the MCP tool |
| 2 | **high** | On-device traffic could escape through an HTTP proxy: `urlopen` honours `$HTTP_PROXY` and does not exclude localhost unless `$NO_PROXY` says so. A proxy would receive the full restricted prompt. | loopback requests use an empty `ProxyHandler` and refuse redirects |
| 3 | **medium** | A backend with **no `egress:` key at all** silently inherited the scheme's claim, so `org/tenant/m` pointing at a public API was accepted for confidential content. `Backend.from_config` separately defaulted to `on-device`. | a missing or unreadable egress resolves to `cloud-public`; the dataclass default is now the least-trusted class |
| 4 | **medium** | Confidential prompts and `Authorization` headers could travel over cleartext `http://` to a non-loopback host, and it validated clean. | non-loopback cleartext degrades to `cloud-public` and is reported |
| 5 | **medium** | `except re.error: continue` silently discarded an invalid sensitivity pattern. A typo'd protection rule vanished while the config still claimed the content was covered. | compile failures are collected and surfaced by `validate()` with the rule index |
| 6 | **medium** | Unknown config keys were ignored, so `sensitivty:` deleted **every detection pattern** and `max_egres:` removed the global cap — both validating clean. | known-key checking on top-level and nested sections |
| 7 | **medium** | Signal masking replaced alphanumerics but kept punctuation verbatim, so a symbol-only credential (`secret=+-*/`) was echoed intact. | every non-space character is masked |
| 8 | **low** | Malformed YAML, bad UTF-8 and non-numeric timeouts escaped as tracebacks. | translated into `ConfigError` / `BackendError` |

## Limitations — read this before trusting it

**Classification happens after the session model has already read the content.**
This plugin routes *execution*. If Scout's cloud session model has already
received a confidential email in its context, calling `route_classify` on it
afterwards cannot un-disclose it — the decision only governs where the
*follow-on work* runs. Genuine prevention would require classification as
host-side middleware ahead of the first model invocation, which is outside what
a plugin can reach. Treat this as a control over delegation and execution, not
as a guarantee that the primary model never sees sensitive text.

**`org-tenant` is asserted, not verified.** Nothing proves an endpoint you
declare as tenant-hosted actually is. The router checks the URL is HTTPS and
non-loopback, and that on-device claims resolve to loopback — it cannot verify
tenancy. That declaration is yours to get right.

**Detection is pattern-based.** It will miss sensitive content that carries no
marker, no label and no M365 provenance. Pass `--label` and `--source` whenever
you have them; they are far stronger signals than the text scan.

### Added for Scout

- **MIP sensitivity labels** — pass `--label "Highly Confidential"` and the
  label drives routing directly. Checked most-restrictive-first, so
  "Highly Confidential" is not swallowed by the "Confidential" substring.
- **M365 provenance** — `--source email|teams|sharepoint|calendar|transcript`
  applies a `confidential` floor. Content read out of the tenant is business
  content even when the text looks benign.
- **Real local execution** — Hermes delegates model execution to its own
  provider layer. Scout's `task` tool hosts only Scout's cloud models, so this
  plugin ships an OpenAI-compatible client covering Ollama, LM Studio, Foundry
  Local, llama.cpp, vLLM and Azure AI Foundry.
- **Backend discovery** — `probe` reports what is reachable, including known
  local runtime ports that are running but unconfigured.
- **`reasoning_effort`** passed through per tier and role.
- **Config validation** — `status` flags a `restricted_model` that is not
  actually on-device, and any reference it cannot parse.
- **Global policy cap** — `policy.max_egress` caps all routing regardless of
  content, for an air-gapped or high-compliance profile.
- **Graded sensitivity** — Hermes has binary normal/sensitive, which cannot
  express "internal business content, tenant-hosted model is fine". That
  distinction comes up constantly in Microsoft 365 work.

## Install

```bash
pip install -r requirements.txt
python -m hybrid_routing init          # writes ~/.scout/hybrid_routing/routing_config.yaml
python -m hybrid_routing probe         # see which local runtimes are already up
```

Edit the config to fill in your models, then:

```bash
python -m hybrid_routing status        # validates and reports problems
python -m hybrid_routing test          # engine self-test
```

See [`setup.md`](setup.md) for the skill and MCP server registration.

## Usage

```bash
python -m hybrid_routing classify "Debug the import logic in this module"
python -m hybrid_routing classify --label "Highly Confidential" --source email "Summarize this thread"
python -m hybrid_routing infer --backend ollama --model qwen3:8b --prompt-file task.txt
```

Add `--json` for machine-readable output. Exit codes: `0` routed, `1` blocked,
`2` not configured, `3` error.

### Example — sensitive M365 content

```
$ python -m hybrid_routing classify --label "Highly Confidential" --source email \
      "Summarize this thread and list open questions"

  sensitivity   : restricted
  egress ceiling: on-device
  MODEL         : local/ollama/qwen3:8b
  channel       : openai-http
  delegate      : YES

  reason: Content classified 'restricted' -> routed to 'local/ollama/qwen3:8b'
          (on-device); egress ceiling 'on-device' enforced; 5 cloud/less-trusted
          model(s) excluded

  signals:
    - MIP label 'Highly Confidential' -> restricted
    - M365 provenance (email) -> confidential floor

  run: python -m hybrid_routing infer --backend ollama --model qwen3:8b --prompt-file <file>
```

### Example — the same content with a cloud-only config

```
  ROUTING BLOCKED
  MODEL         : (none selected)

  reason: BLOCKED: content classified 'restricted' requires a model at or inside
          the 'on-device' boundary, and none is configured. 5 configured model(s)
          were rejected because their egress class exceeds the 'on-device'
          boundary. Routing to an available cloud model would send this content
          outside the required boundary.

  excluded (outside ceiling):
    x scout/cloud-balanced  — egress 'cloud-public' exceeds ceiling 'on-device'
```

Exit code `1`. This is the case the upstream plugin silently sends to the cloud.

## Tools (via MCP)

Registering the MCP server puts these in Scout's tool list on every turn:

| Tool | Purpose |
|---|---|
| `route_classify` | classify a task and get a routing decision |
| `route_status` | show configuration and validation problems |
| `route_test` | run the engine self-test |
| `route_probe` | report reachable local / org backends |
| `route_infer` | run a prompt on a local or org backend |

## Architecture

```
hybrid_routing/
├── egress.py       egress classes, model reference parsing  ← the trust model
├── classify.py     sensitivity / role / difficulty, MIP labels, provenance
├── router.py       selection + egress filtering + fail-closed blocking
├── backends.py     OpenAI-compatible client, discovery (stdlib only)
├── config.py       config resolution and validation
├── selftest.py     engine self-test against a pinned reference config
├── cli.py          command line interface
└── mcp_server.py   MCP stdio server (JSON-RPC, no dependencies)

data/routing_config.yaml   shipped default — every model field blank
skill/                     Scout skill package
tests/                     143 tests
```

## Design notes

**Why blank by default.** A router that guesses is a router that eventually
sends something somewhere you did not intend. Nothing routes until you name a
model. Unconfigured is reported, not silently defaulted.

**Why unknown schemes raise.** An unrecognized reference could be local or
cloud. Guessing wrong on that single question is the entire failure mode this
plugin exists to prevent, so it refuses to guess.

**Why local models always delegate.** `skip_for_tier: [fast]` can only apply to
Scout-hosted models. A local model is unreachable from the primary session, so
it is always executed out of band regardless of tier.

**Why signals are redacted.** Classification signals name the pattern that
matched and echo at most 12 characters of it, so a decision record is auditable
without reprinting the secret that triggered it.

## Testing

```bash
python -m pytest        # 143 tests
```

## License

MIT — © 2026 Michael Gannotti. Derivative of `smfworks/hermes-plugin-hybrid-routing`,
MIT © 2026 SMF Works. See [LICENSE](LICENSE) and [ATTRIBUTION.md](ATTRIBUTION.md).


