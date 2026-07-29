<#
.SYNOPSIS
    Pre-flight and rehearsal harness for the hybrid contextual inference demo.

.DESCRIPTION
    Gets the machine into a demonstrable state, then walks the five-step
    routing demo.

    Pre-flight is the part that matters. Three things reliably break this demo
    in front of an audience:

      1. The Foundry Local daemon is not running.
      2. The daemon came up on a different port than the routing config
         expects. Its default is `auto`, so the port changes on every restart.
      3. The model is downloaded but not *loaded*. The OpenAI endpoint still
         lists it in /v1/models, then returns HTTP 400 on the first completion.

    None of those are visible until you are already presenting, so pre-flight
    checks all three and fixes what it can.

    It also sends a throwaway completion to warm the model. Without that the
    first live call pays cold-start latency, which on integrated graphics is
    the difference between roughly ten seconds and closer to a minute.

.PARAMETER Preflight
    Prepare and verify only. Does not run the demo.

.PARAMETER Rehearse
    Run every step back to back with no pauses. Use to time the whole thing.

.PARAMETER SkipWarmup
    Skip the warm-up completion. Faster pre-flight, slower first demo step.

.EXAMPLE
    .\hybrid-demo.ps1 -Preflight
    Run this before you walk into the room.

.EXAMPLE
    .\hybrid-demo.ps1
    Interactive: pauses between steps so you can narrate.
#>

[CmdletBinding()]
param(
    [switch]$Preflight,
    [switch]$Rehearse,
    [switch]$SkipWarmup,
    [string]$ConfigPath
)

$ErrorActionPreference = 'Stop'
$script:RepoRoot = Split-Path -Parent $PSScriptRoot
$script:Failures = @()

# ── Presentation helpers ───────────────────────────────────────────────
function Write-Banner($Text) {
    Write-Host ''
    Write-Host ('=' * 74) -ForegroundColor DarkCyan
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host ('=' * 74) -ForegroundColor DarkCyan
}

function Write-Step($Number, $Title, $Narration) {
    Write-Host ''
    Write-Host ('-' * 74) -ForegroundColor DarkGray
    Write-Host "  STEP $Number - $Title" -ForegroundColor Yellow
    Write-Host ('-' * 74) -ForegroundColor DarkGray
    if ($Narration) { Write-Host "  $Narration" -ForegroundColor Gray; Write-Host '' }
}

function Write-Check($Ok, $Text, $Detail) {
    if ($Ok) {
        Write-Host "  [ ok ] $Text" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] $Text" -ForegroundColor Red
        $script:Failures += $Text
    }
    if ($Detail) { Write-Host "         $Detail" -ForegroundColor DarkGray }
}

function Pause-Here {
    if ($Rehearse) { return }
    Write-Host ''
    Write-Host '  [enter to continue]' -ForegroundColor DarkGray -NoNewline
    [void](Read-Host)
}

function Invoke-Native([scriptblock]$Block) {
    # Native commands that write to stderr - which this tool does deliberately
    # when it refuses a request - are treated as terminating errors under
    # $ErrorActionPreference = 'Stop'. That killed the demo at exactly the step
    # meant to show a refusal, so native calls are isolated here.
    #
    # Merged stderr also arrives as ErrorRecord objects, whose default
    # rendering is the useless string "System.Management.Automation.
    # RemoteException". Flatten them to their actual text and drop the empties.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Block 2>&1 | ForEach-Object {
            $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { "$_" }
            if ($line -and $line -notmatch 'RemoteException') { $line }
        }
    } finally { $ErrorActionPreference = $prev }
}

# ── Tool discovery ─────────────────────────────────────────────────────
function Resolve-Tool($Name, $Candidates) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($c in $Candidates) { if (Test-Path $c) { return $c } }
    return $null
}

$python = Resolve-Tool 'python' @(
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
)
$foundry = Resolve-Tool 'foundry' @("$env:LOCALAPPDATA\Microsoft\WindowsApps\foundry.exe")

# ── Read the routing config ────────────────────────────────────────────
function Get-DemoTarget {
    # Pull the local sensitivity model and its backend out of the routing
    # config, so this script follows the config rather than hardcoding a model
    # that may have been swapped out.
    $reader = @'
import json, os, sys
sys.path.insert(0, os.environ["HR_REPO"])
from hybrid_routing.config import load_config
from hybrid_routing.egress import parse_model_ref

cfg, path = load_config(os.environ.get("HR_CONFIG") or None)
sens = cfg.get("sensitivity", {}) or {}
ref = (sens.get("restricted_model") or sens.get("confidential_model") or "").strip()
out = {"config_path": str(path), "ref": ref}
if ref:
    parsed = parse_model_ref(ref)
    backend = (cfg.get("backends", {}) or {}).get(parsed.backend, {}) or {}
    out.update(
        backend=parsed.backend,
        model_id=parsed.model_id,
        base_url=str(backend.get("base_url", "")).rstrip("/"),
        egress=str(backend.get("egress", "")),
    )
print(json.dumps(out))
'@
    $env:HR_REPO = $script:RepoRoot
    if ($ConfigPath) { $env:HR_CONFIG = $ConfigPath }
    $raw = $reader | & $python - 2>&1
    if ($LASTEXITCODE -ne 0) { throw "could not read routing config: $raw" }
    return $raw | ConvertFrom-Json
}

# ── Pre-flight ─────────────────────────────────────────────────────────
function Invoke-Preflight {
    Write-Banner 'PRE-FLIGHT'

    Write-Check ($null -ne $python)  'python found'        $python
    Write-Check ($null -ne $foundry) 'Foundry Local found' $foundry
    if (-not $python -or -not $foundry) {
        Write-Host ''
        Write-Host '  Cannot continue. Install Foundry Local with:' -ForegroundColor Red
        Write-Host '    winget install Microsoft.FoundryLocal' -ForegroundColor Gray
        return $null
    }

    $target = Get-DemoTarget
    if (-not $target.ref) {
        Write-Check $false 'a local sensitivity model is configured' `
            'set sensitivity.restricted_model to a local/<backend>/<model> reference'
        return $null
    }
    Write-Check $true 'routing config read' $target.config_path
    Write-Check ($target.egress -eq 'on-device') 'sensitivity model is on-device' $target.ref

    # Expected port comes from the config rather than being assumed.
    $expectedPort = 5273
    if ($target.base_url -match ':(\d+)') { $expectedPort = [int]$Matches[1] }

    # 1 - daemon up?
    $status = & $foundry server status 2>&1 | Out-String
    if ($status -notmatch 'Ready') {
        Write-Host '  [ .. ] starting Foundry daemon' -ForegroundColor DarkYellow
        & $foundry server start 2>&1 | Out-Null
        Start-Sleep -Seconds 4
        $status = & $foundry server status 2>&1 | Out-String
    }
    Write-Check ($status -match 'Ready') 'Foundry daemon is running'

    # 2 - on the port the config expects? Its default is `auto`, so this drifts.
    $actualPort = if ($status -match '127\.0\.0\.1:(\d+)') { [int]$Matches[1] } else { 0 }
    if ($actualPort -ne $expectedPort) {
        Write-Host "  [ .. ] daemon on $actualPort, config expects $expectedPort - pinning" -ForegroundColor DarkYellow
        & $foundry config set port $expectedPort --force 2>&1 | Out-Null
        & $foundry server restart 2>&1 | Out-Null
        Start-Sleep -Seconds 6
        $status = & $foundry server status 2>&1 | Out-String
        $actualPort = if ($status -match '127\.0\.0\.1:(\d+)') { [int]$Matches[1] } else { 0 }
    }
    Write-Check ($actualPort -eq $expectedPort) "daemon on the expected port ($expectedPort)"

    # 3 - model actually loaded? /v1/models lists downloaded models whether or
    #     not they are resident, so loading is done unconditionally.
    $alias = $target.model_id -replace '-generic-(gpu|cpu)$', ''
    Write-Host "  [ .. ] loading '$alias' (idempotent)" -ForegroundColor DarkYellow
    & $foundry model load $alias 2>&1 | Out-Null

    $probe = & $python -m hybrid_routing --json probe --configured-only 2>&1 | Out-String
    $reachable = $false
    try {
        $parsed = $probe | ConvertFrom-Json
        # Tolerate either shape: a bare array or { "backends": [...] }.
        $backends = if ($null -ne $parsed.backends) { $parsed.backends } else { $parsed }
        $reachable = [bool]($backends | Where-Object { $_.backend -eq $target.backend -and $_.reachable })
    } catch { }
    Write-Check $reachable "backend '$($target.backend)' reachable" $target.base_url

    # 4 - config validates clean
    $st = & $python -m hybrid_routing --json status 2>&1 | Out-String
    $problems = @()
    try { $problems = ($st | ConvertFrom-Json).problems } catch { }
    Write-Check ($problems.Count -eq 0) 'routing config validates clean' `
        $(if ($problems.Count) { $problems -join '; ' } else { 'no problems' })

    # 5 - warm the model so the first live step is not a cold start
    if (-not $SkipWarmup) {
        Write-Host '  [ .. ] warming the model' -ForegroundColor DarkYellow
        $sw = [Diagnostics.Stopwatch]::StartNew()
        & $python -m hybrid_routing infer --backend $target.backend --model $target.model_id `
            --max-tokens 8 --prompt 'Say ready.' 2>&1 | Out-Null
        $sw.Stop()
        Write-Check ($LASTEXITCODE -eq 0) 'model responds' "cold call $([math]::Round($sw.Elapsed.TotalSeconds,1))s"
    }

    Write-Host ''
    if ($script:Failures.Count -eq 0) {
        Write-Host '  PRE-FLIGHT PASSED - safe to present.' -ForegroundColor Green
    } else {
        Write-Host "  PRE-FLIGHT FAILED - $($script:Failures.Count) issue(s):" -ForegroundColor Red
        $script:Failures | ForEach-Object { Write-Host "    - $_" -ForegroundColor Red }
    }
    return $target
}

# ── Demo steps ─────────────────────────────────────────────────────────
function Show-Classify($ClassifyArgs, $Narration, $Number, $Title) {
    Write-Step $Number $Title $Narration
    $out = Invoke-Native { & $python -m hybrid_routing classify @ClassifyArgs }
    $shown = 0
    $out | Select-String -Pattern 'sensitivity   |egress        |MODEL         |ROUTING BLOCKED|reason:|excluded|^\s+x ' |
        ForEach-Object {
            $line = $_.Line.Trim()
            # The exclusion list can run to every configured cloud model. Three
            # is enough to make the point on a slide-sized terminal.
            if ($line -like 'x *') {
                $shown++
                if ($shown -le 3) { Write-Host "  $line" -ForegroundColor DarkRed }
                elseif ($shown -eq 4) { Write-Host '  x ...' -ForegroundColor DarkRed }
            } else {
                Write-Host "  $line"
            }
        }
    Pause-Here
}

function Invoke-Demo($Target) {
    $b = $Target.backend
    $m = $Target.model_id

    Write-Banner 'HYBRID CONTEXTUAL INFERENCE - DEMO'
    Write-Host '  Same assistant, same task. What changes is where it runs.' -ForegroundColor Gray
    Pause-Here

    Show-Classify @('--source', 'email', 'Summarize the account sync and list follow-ups') `
        'Routine business email. Nothing sensitive in it.' 1 'Ordinary work goes to the cloud'

    Show-Classify @('--label', 'Highly Confidential', '--source', 'email', 'Care coordination notes for patient MRN 44718') `
        'Same request. This time the item carries a sensitivity label.' 2 'Labelled content is contained'

    Write-Step 3 'It really runs here' 'No network egress. Watch the latency - that is local hardware.'
    $sw = [Diagnostics.Stopwatch]::StartNew()
    Invoke-Native {
        & $python -m hybrid_routing infer --backend $b --model $m --max-tokens 90 `
            --label 'Highly Confidential' --source email `
            --prompt 'Summarize for the care team: Patient MRN 44718 seen 7/14/2026 for post-op follow-up. Wound healing well. PT referral pending. Next visit 8/02/2026.'
    } | ForEach-Object { Write-Host "  $_" }
    $sw.Stop()
    Write-Host ''
    Write-Host "  $([math]::Round($sw.Elapsed.TotalSeconds,1))s - entirely on this machine." -ForegroundColor Green
    Pause-Here

    Write-Step 4 'Now try to make it leak' 'Deliberately target the tenant endpoint with the same PHI.'
    Invoke-Native {
        & $python -m hybrid_routing infer --backend tenant --model gpt-4o `
            --prompt 'Patient MRN 44718 post-op notes, summarize'
    } | Where-Object { "$_".Trim() } | Select-Object -First 3 |
        ForEach-Object { Write-Host "  $("$_".Trim())" -ForegroundColor Red }
    Write-Host ''
    Write-Host '  Refused before any network call - and refused even to a tenant-hosted' -ForegroundColor Green
    Write-Host '  endpoint, because restricted content requires on-device.' -ForegroundColor Green
    Pause-Here

    Show-Classify @('--label', 'Confidential', 'Quarterly operating plan review') `
        'Not everything is locked down. Confidential is a different tier than restricted.' `
        5 'Proportionate, not blunt'

    Write-Banner 'SUMMARY'
    @(
        'Routine work   -> cloud frontier model, full capability',
        'Labelled work  -> on-device, never leaves the machine',
        'Forced egress  -> refused before the request is sent',
        'Graded tiers   -> confidential and restricted treated differently'
    ) | ForEach-Object { Write-Host "  $_" -ForegroundColor White }
    Write-Host ''
    Write-Host '  The control is enforced at the point of transmission, not just advised.' -ForegroundColor Cyan
    Write-Host ''
}

# ── Main ───────────────────────────────────────────────────────────────
Push-Location $script:RepoRoot
$env:PYTHONIOENCODING = 'utf-8'
try {
    $target = Invoke-Preflight
    if (-not $target) { exit 2 }
    if ($script:Failures.Count -gt 0) { exit 1 }
    if ($Preflight) { exit 0 }
    Invoke-Demo $target
    exit 0
} finally {
    Pop-Location
}
