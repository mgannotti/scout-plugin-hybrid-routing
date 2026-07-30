# Setup

## 1. Dependencies

```bash
cd "<repo>"
pip install -r requirements.txt
```

PyYAML is the only runtime dependency. The MCP server and the backend client
are standard library only.

## 2. Config

```bash
python -m hybrid_routing init
```

Writes `~/.scout/hybrid_routing/routing_config.yaml`. Every model field is
blank — nothing routes until you fill them in.

Resolution order: `--config` → `$SCOUT_HYBRID_ROUTING_CONFIG` →
`~/.scout/hybrid_routing/routing_config.yaml` → the shipped default.

## 3. Find your local runtime

```bash
python -m hybrid_routing probe
```

Reports configured backends plus any well-known local runtime already
listening:

| Runtime | Default port | Notes |
|---|---|---|
| Ollama | 11434 | `ollama serve`; OpenAI API at `/v1` |
| LM Studio | 1234 | enable the local server in the app |
| Foundry Local | 5273 | `foundry service status` — port varies |
| llama.cpp | 8080 | `llama-server --port 8080` |
| vLLM | 8000 | `vllm serve <model>` |

If nothing is running and you want an on-device tier, Foundry Local is the
Microsoft-aligned option on Windows:

```powershell
winget install Microsoft.FoundryLocal
foundry model run phi-4-mini
foundry service status        # note the port, then set it in the config
```

On a machine without a discrete GPU, keep on-device models small (3B–8B class)
and use them for the fast tier and for sensitive summarization — not for deep
reasoning.

## 4. Fill in the config

Model reference format:

```
scout/<model-id>              cloud, via Scout's task tool
local/<backend>/<model-id>    on-device
org/<backend>/<model-id>      org-hosted
```

`<backend>` names an entry under `backends:`, which is where the URL and auth
live. **Never put an API key in the config** — set `api_key_env` to the name of
an environment variable.

A worked hybrid example:

```yaml
tiers:
  fast:     { model: "local/ollama/phi4-mini", reasoning_effort: low }
  balanced: { model: "scout/cloud-balanced",      reasoning_effort: medium }
  strong:   { model: "scout/cloud-strong",    reasoning_effort: high }

roles:
  coding:   { model: "scout/cloud-coder" }
  strategy: { model: "scout/cloud-strong" }

sensitivity:
  confidential_model: "org/tenant/gpt-4o"
  restricted_model:   "local/ollama/qwen3:8b"

delegation:
  primary_model: "scout/cloud-strong"     # your actual session model
```

Set `delegation.primary_model` to the model your Scout session really runs
(`m_get_current_model`), otherwise the router cannot tell when it can stay
inline and will delegate more than necessary.

Then validate:

```bash
python -m hybrid_routing status
python -m hybrid_routing test
```

`status` flags a `restricted_model` that is not actually on-device, a
`confidential_model` outside the tenant boundary, and any reference it cannot
parse.

## 5. Register the MCP server (recommended)

This is what gives Scout the always-available `route_*` tools, matching the
Hermes plugin's native tool registration.

Add to `~/.scout/m-mcp-servers.json` under `servers`:

```json
"hybrid-routing": {
  "builtin": false,
  "config": {
    "name": "hybrid-routing",
    "type": "command",
    "command": "<absolute path to python.exe>",
    "args": ["<absolute path to this repo>/mcp_launcher.py"]
  },
  "tools": ["*", "route_classify", "route_status", "route_test", "route_probe", "route_infer"]
}
```

Use **absolute paths for both**, and launch `mcp_launcher.py` rather than
`-m hybrid_routing.mcp_server`.

Scout normalizes this entry when it loads it, and **drops `cwd` and `env`**
without warning. A `-m` invocation then fails with `ModuleNotFoundError: No
module named 'hybrid_routing'`, because nothing ever put the package on
`sys.path`. The launcher sits at the repo root and bootstraps its own import
path, so it works with no working directory and no environment.

(If you have installed the package with `pip install -e .`, then
`"args": ["-m", "hybrid_routing.mcp_server"]` works too.)

Then enable it. Scout does not auto-enable a hand-added server, so add an entry
under `permissions.servers` in `~/.scout/m-settings.json`:

```json
"hybrid-routing": { "enabled": true, "autoApprove": false }
```

Restart Scout and verify by asking it to run `route_status`.

Sanity-check the server without Scout first — from a *different* directory, to
prove it does not depend on the working directory:

```powershell
cd C:\
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python "<repo>\mcp_launcher.py"
```

You should get a JSON response listing five tools. If you get a traceback, the
paths in the config are wrong.

## 6. Install the skill

Copy `skill/` into your Scout skills directory as `hybrid-routing`:

```powershell
$dest = "$HOME\.scout\m-skills\hybrid-routing"
New-Item -ItemType Directory -Force -Path $dest
Copy-Item skill\* $dest -Recurse -Force
```

Invoke with `/hybrid-routing`.

The skill works without the MCP server (it shells out to the CLI), and the MCP
server works without the skill. Installing both is recommended: the tools are
available every turn, and the skill carries the guidance on how to act on a
decision.

## Optional: org-hosted endpoint

Any OpenAI-compatible endpoint inside your tenant or network:

```yaml
backends:
  tenant:
    base_url: "https://<resource>.services.ai.azure.com/openai/v1"
    egress: org-tenant
    api_key_env: "AZURE_AI_FOUNDRY_KEY"
```

```powershell
[Environment]::SetEnvironmentVariable("AZURE_AI_FOUNDRY_KEY", "<key>", "User")
```

`egress` must be accurate. It is the safety boundary, not a label — marking a
public endpoint as `org-tenant` defeats the enforcement.

## Optional: lock everything down

```yaml
policy:
  max_egress: "on-device"      # or org-tenant
```

Caps all routing regardless of content classification.

## Optional: tune the M365 provenance floor

By default, anything you tag `--source email|teams|sharepoint|…` is raised to
`confidential` on the strength of its origin alone. That is the right default
for regulated work, but on a personal setup it routes routine email summaries
to your smallest model.

```yaml
provenance:
  mode: advisory        # floor (default) | advisory | off
  sources:
    transcript: restricted    # still floor meeting transcripts
```

Under `advisory` the source is recorded in the decision signals but does not
raise the level; MIP labels and content patterns still apply in full, so
genuinely sensitive material still routes correctly.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `NOT CONFIGURED` | every model field is blank; fill in the config |
| `ROUTING BLOCKED` | working as designed — no model inside the required boundary |
| Everything from M365 blocks | `provenance.mode` is `floor` (the default) and no org/local model is configured — set `advisory`, or configure a sensitivity model |
| `unknown scheme` | reference must start with `scout/`, `local/` or `org/` |
| backend `down` in probe | runtime not running, or wrong port |
| `expects an API key in $VAR, which is unset` | set the environment variable and restart Scout |
| everything delegates | set `delegation.primary_model` to your session model |
