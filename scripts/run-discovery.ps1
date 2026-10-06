<#
.SYNOPSIS
    Runs community discovery with real-time progress reporting.

.DESCRIPTION
    This script:
    1. Checks if the model server is running (requires separate start)
    2. Runs community discovery with resumable state
    3. Reports each community as it's discovered
    4. Stops after 200 communities or dead end
    5. Saves state so it can be restarted without duplicate work

    IMPORTANT: Start the model server first in a separate window:
    .\scripts\start-model-server.ps1

.PARAMETER MaxCommunities
    Maximum number of communities to discover (default: 200)

.PARAMETER DeadEndThreshold
    Stop after N consecutive pages with no new communities (default: 10)

.PARAMETER Verify
    Verify each discovered community with the LLM (slower but more accurate)

.PARAMETER Reset
    Reset discovery state and start fresh

.EXAMPLE
    .\scripts\run-discovery.ps1
    # Run discovery with defaults (200 communities, no verification)

.EXAMPLE
    .\scripts\run-discovery.ps1 -Verify -MaxCommunities 100
    # Discover and verify up to 100 communities

.EXAMPLE
    .\scripts\run-discovery.ps1 -Reset
    # Start fresh, ignoring previous state
#>
param(
    [int]$MaxCommunities = 200,
    [int]$DeadEndThreshold = 10,
    [switch]$Verify,
    [switch]$Reset
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$HealthUrl = "http://localhost:8080/health"
$StatePath = Join-Path $ProjectRoot "data\communities\discovery_state.json"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Community Discovery Pipeline" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check if Python venv exists
if (-not (Test-Path -LiteralPath $Python)) {
    Write-Error "Python venv not found at $Python. Run 'python -m venv .venv' and install requirements first."
}

# Check model server health
function Test-ServerHealth {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 $HealthUrl
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

Write-Host "[1/3] Checking model server..." -ForegroundColor Yellow

if (Test-ServerHealth) {
    Write-Host "  Model server already healthy at $HealthUrl" -ForegroundColor Green
} else {
    Write-Host "  Model server not running." -ForegroundColor Red
    Write-Host ""
    Write-Host "  Please start the model server in a separate window:" -ForegroundColor Yellow
    Write-Host "    .\scripts\start-model-server.ps1" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Then run this script again." -ForegroundColor Yellow
    Write-Error "Model server not running. Start it first with .\scripts\start-model-server.ps1"
}

# Check for existing state
Write-Host ""
Write-Host "[2/3] Preparing discovery..." -ForegroundColor Yellow

if ((Test-Path -LiteralPath $StatePath) -and -not $Reset) {
    $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    $discoveredCount = $state.discovered_communities.Count
    $processedUrls = $state.processed_urls.Count
    $processedQueries = $state.processed_queries.Count
    
    Write-Host "  Resuming from saved state:" -ForegroundColor Cyan
    Write-Host "    - Communities discovered: $discoveredCount" -ForegroundColor Cyan
    Write-Host "    - URLs processed: $processedUrls" -ForegroundColor Cyan
    Write-Host "    - Queries processed: $processedQueries" -ForegroundColor Cyan
    
    if ($state.stopped_reason) {
        Write-Host "    - Previous stop reason: $($state.stopped_reason)" -ForegroundColor Yellow
    }
} elseif ($Reset) {
    Write-Host "  Resetting state (starting fresh)..." -ForegroundColor Yellow
    if (Test-Path -LiteralPath $StatePath) {
        Remove-Item -LiteralPath $StatePath -Force
    }
} else {
    Write-Host "  Starting fresh discovery..." -ForegroundColor Cyan
}

# Build discovery command
$discoveryArgs = @(
    "-m", "modules.community.discovery",
    "--max-communities", $MaxCommunities,
    "--dead-end-threshold", $DeadEndThreshold,
    "--state-path", $StatePath
)

if ($Verify) {
    $discoveryArgs += "--verify"
}

if ($Reset) {
    $discoveryArgs += "--reset"
}

Write-Host ""
Write-Host "[3/3] Running discovery..." -ForegroundColor Yellow
Write-Host "  Max communities: $MaxCommunities" -ForegroundColor Gray
Write-Host "  Dead end threshold: $DeadEndThreshold pages" -ForegroundColor Gray
Write-Host "  Verification: $(if ($Verify) { 'enabled' } else { 'disabled' })" -ForegroundColor Gray
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Run discovery
Push-Location $ProjectRoot
try {
    & $Python @discoveryArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Discovery exited with code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Discovery Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan

# Show final state
if (Test-Path -LiteralPath $StatePath) {
    $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    Write-Host ""
    Write-Host "Final Statistics:" -ForegroundColor Cyan
    Write-Host "  - Total discovered: $($state.discovered_communities.Count)" -ForegroundColor Green
    Write-Host "  - Total verified: $($state.verified_communities.Count)" -ForegroundColor Green
    Write-Host "  - URLs processed: $($state.processed_urls.Count)" -ForegroundColor Gray
    Write-Host "  - Queries processed: $($state.processed_queries.Count)" -ForegroundColor Gray
    
    if ($state.stopped_reason) {
        Write-Host ""
        Write-Host "Stopped: $($state.stopped_reason)" -ForegroundColor Yellow
        Write-Host "To continue: Run this script again without -Reset" -ForegroundColor Yellow
    }
}
