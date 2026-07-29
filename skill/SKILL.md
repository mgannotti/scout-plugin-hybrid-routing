---
name: hybrid-routing
description: >
  Route a task to the right model across a hybrid stack — Scout's hosted cloud
  models, on-device runtimes (Ollama, LM Studio, Foundry Local, llama.cpp,
  vLLM), and org-hosted endpoints — by classifying it on sensitivity, role and
  difficulty, and enforcing an egress boundary so confidential and restricted
  content never leaves the boundary it belongs in. Trigger when the user says
  /hybrid-routing, "which model should handle this", "route this task", "run
  this locally", "keep this off the cloud", "use a local model", "set up hybrid
  inference", or asks why a task went to a particular model.
version: 1.0.0
author: Michael Gannotti
license: MIT
---

# Hybrid Contextual Inference

Pick the right model for the task, and keep sensitive content inside the trust
boundary it belongs in.

Scout's session runs one model. This skill adds per-task contextual routing:
classify the task on three signals, select a model, and either delegate to a
Scout subagent (cloud) or call an on-device / org-hosted endpoint directly.
The primary session model never changes, so prompt caching survives.

## The three signals

| Signal | Values | Drives |
|---|---|---|
| **sensitivity** | `normal` / `confidential` / `restricted` | the egress ceiling |
| **role** | coding, research, creative, strategy, vision, general | model specialization |
| **difficulty** | simple / standard / hard | fast / balanced / strong tier |

Sensitivity is computed from three inputs together, and the most restrictive
wins: patterns in the text, the Microsoft Information Protection label if the
content carries one, and the M365 surface it came from.

## Egress classes — the part that matters

Every model reference declares where inference physically happens:

| Scheme | Egress | Executed via |
|---|---|---|
| `scout/<model-id>` | `cloud-public` | Scout's `task` tool |
| `org/<backend>/<model>` | `org-tenant` | OpenAI-compatible HTTP |
| `local/<backend>/<model>` | `on-device` | OpenAI-compatible HTTP |

Sensitivity sets a **ceiling** and both selection and fallback are filtered by
it:

```
normal        -> cloud-public permitted
confidential  -> org-tenant or tighter
restricted    -> on-device only
```

**If nothing satisfies the ceiling, the router BLOCKS.** It does not fall back
to a cloud model. A blocked decision is a correct decision — report it and
handle the task another way.

## How to use it

### Step 1 — classify

If the MCP server is registered, call the tool directly:

```
route_classify(text="<the task>", label="<MIP label>", source="<email|teams|...>")
```

Otherwise shell out:

```bash
python -m hybrid_routing classify "<the task>" --label "Confidential" --source email
```

Always pass `label` when the content came from a labelled M365 item, and
`source` when it came from Outlook, Teams, SharePoint, OneDrive, a calendar
item or a meeting transcript. Without those the router only sees the text, and
a benign-looking summary request over confidential material will classify as
`normal`.

### Step 2 — act on `execution`

The decision carries an `execution` block that tells you exactly what to run.

**Cloud (`channel: scout-task`)** — delegate with Scout's task tool:

```
task(agent_type="general-purpose", model="<model_id>", reasoning_effort="<effort>", prompt=...)
```

Use `model_id` (bare, e.g. `cloud-coder`), not the full `scout/...` reference.
If `should_delegate` is false, the selected model is the session model — just
answer inline.

**Local or org (`channel: openai-http`)** — Scout's task tool cannot host these
models. Call the backend:

```
route_infer(backend="<backend>", model="<model_id>", prompt="<the task>")
```

or:

```bash
python -m hybrid_routing infer --backend <backend> --model <model_id> --prompt-file <file>
```

Prefer `--prompt-file` over `--prompt` for sensitive content so it never lands
in shell history.

**Blocked (`status: blocked`)** — do not route it anywhere. Tell the user what
was classified, what boundary it needs, and that no in-boundary model is
configured. Offer to configure one. Never "just use the cloud model this once".

### Step 3 — report the routing, briefly

State the model and why in one line. Users should be able to see when their
content stayed on-device.

## Commands

```bash
python -m hybrid_routing status            # what is configured, and any problems
python -m hybrid_routing probe             # which backends are reachable now
python -m hybrid_routing classify "..."    # classify and route
python -m hybrid_routing test              # engine self-test
python -m hybrid_routing init              # install the user config
python -m hybrid_routing infer --backend ollama --model qwen3:8b --prompt "..."
```

Add `--json` to any command for machine-readable output.

Exit codes: `0` routed, `1` blocked, `2` not configured, `3` error.

## Configuration

User config lives at `~/.scout/hybrid_routing/routing_config.yaml`. Every model
field ships blank — the router reports `unconfigured` rather than guessing.

Run `python -m hybrid_routing status` after editing. It validates that the
`restricted_model` is genuinely on-device and the `confidential_model` is
genuinely at or inside the tenant boundary, and reports any reference it cannot
parse.

Set `policy.max_egress` to `org-tenant` or `on-device` to cap **all** routing
regardless of content — an air-gapped or high-compliance profile.

## Guardrails

- Never route content above its ceiling, even if the user asks. Explain the
  block instead.
- Never treat a blocked decision as a failure to work around.
- Never write an API key into the config. Backends name an environment
  variable via `api_key_env`.
- The router classifies text you give it. If you only pass a paraphrase, it
  classifies the paraphrase — pass the actual content, or pass the label.
- An unparseable model reference is rejected, never guessed. A scheme the
  router does not recognize could be local or cloud, and guessing wrong on that
  is the whole failure mode this skill exists to prevent.
