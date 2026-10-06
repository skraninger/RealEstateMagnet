<#
.SYNOPSIS
    One-command live test for Workstream B (community research agent on the local model).

.DESCRIPTION
    1. Checks the llama.cpp server health at http://localhost:8080/health;
       starts it via start-model-server.ps1 if it is not running.
    2. Runs the research engine on ONE community (default "Pelican Bay").
    3. Prints the JSON report and the files written to data\communities\.

    IMPORTANT: do NOT run this while opencode itself is being served by the
    local llama-server with the same model; a second 16 GB instance will
    contend for VRAM/RAM and can crash the session. Run opencode on a
    non-local (cloud) model while testing (see Documents/WorkstreamB_LiveTest_Resume.md).

.PARAMETER Community
    Target community name, substring match against data\communities\target_list.json.

.PARAMETER SkipStart
    Never start the server; fail fast if it is not already healthy.
#>
param(
    [string]$Community = "Pelican Bay",
    [switch]$SkipStart
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$HealthUrl = "http://localhost:8080/health"

function Test-ServerHealth {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 $HealthUrl
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

if (Test-ServerHealth) {
    Write-Host "[1/3] Model server already healthy at $HealthUrl"
} elseif ($SkipStart) {
    Write-Error "Model server not healthy and -SkipStart was set. Start it with .\scripts\start-model-server.ps1 first."
} else {
    Write-Host "[1/3] Model server not running; starting (first load of the 16 GB model takes a few minutes)..."
    & (Join-Path $PSScriptRoot "start-model-server.ps1")
    if ($LASTEXITCODE -ne 0) { Write-Error "Model server failed to start." }
}

Write-Host "[2/3] Researching '$Community' (this makes real web searches + LLM calls; expect several minutes)..."
Push-Location $ProjectRoot
try {
    & $Python -m modules.community.research_engine --community $Community
    if ($LASTEXITCODE -ne 0) { Write-Error "research_engine exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}

Write-Host "[3/3] Artifacts:"
$slug = ($Community -replace '[^a-zA-Z0-9]+', '-').ToLower().Trim('-')
$record = Join-Path $ProjectRoot "data\communities\communities\$slug.json"
$log = Join-Path $ProjectRoot "data\communities\research_log.jsonl"
if (Test-Path -LiteralPath $record) {
    Write-Host "  record: $record"
} else {
    Write-Warning "  record not found: $record"
}
if (Test-Path -LiteralPath $log) {
    $lines = (Get-Content -LiteralPath $log).Count
    Write-Host "  research log: $log ($lines entries total)"
}
Write-Host "Done. Success = report line shows fees/amenities/proximity > 0 and no errors."
