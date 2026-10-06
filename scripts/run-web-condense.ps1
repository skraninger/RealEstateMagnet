<#
.SYNOPSIS
    Runs the Web-based AI Condenser to accumulate community data from web search.

.DESCRIPTION
    This script uses web search (DuckDuckGo) + local model to accumulate community data.
    It works like Gemini in a browser: searches the web, reads pages, and has the local
    model extract structured information.

    Unlike the AI Condenser (which uses AI knowledge directly), this approach:
    1. Searches the web for each query
    2. Reads the actual web pages
    3. Local model extracts structured data from the content
    4. Loops through queries to accumulate comprehensive data

    This avoids needing a Gemini API key while still leveraging web-based research.

.PARAMETER Mode
    Operation mode:
    - discover: Search web for communities (default)
    - enrich: Read web pages to add details to known communities
    - accumulate: Discover + enrich in a loop for comprehensive data
    - summary: Show database statistics only

.PARAMETER Limit
    Maximum communities to process (default: 50)

.PARAMETER Community
    Enrich a specific community by name

.PARAMETER Verbose
    Show prompts sent to local model and extracted data

.PARAMETER ShowThinking
    Show detailed extraction process

.PARAMETER Reset
    Delete existing database and start fresh

.EXAMPLE
    .\scripts\run-web-condense.ps1
    # Show database summary

.EXAMPLE
    .\scripts\run-web-condense.ps1 -Mode discover -Limit 20
    # Discover up to 20 communities from web search

.EXAMPLE
    .\scripts\run-web-condense.ps1 -Mode enrich -Limit 10
    # Enrich up to 10 known communities from web content

.EXAMPLE
    .\scripts\run-web-condense.ps1 -Mode accumulate -Limit 100 -Verbose
    # Full accumulation loop with detailed output

.EXAMPLE
    .\scripts\run-web-condense.ps1 -Community "Pelican Bay" -ShowThinking
    # Enrich a single community with detailed extraction
#>
param(
    [ValidateSet("discover", "enrich", "accumulate", "summary")]
    [string]$Mode = "summary",

    [int]$Limit = 50,

    [string]$Community = "",

    [switch]$Verbose,

    [switch]$ShowThinking,

    [switch]$Reset
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$DbPath = Join-Path $ProjectRoot "data\communities.db"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Web-based AI Condenser" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Error "Python venv not found at $Python. Run 'python -m venv .venv' and install requirements first."
}

if ($Reset -and (Test-Path -LiteralPath $DbPath)) {
    Write-Host "[RESET] Removing existing database..." -ForegroundColor Yellow
    Remove-Item -LiteralPath $DbPath -Force
}

$condenserArgs = @(
    "-m", "modules.community.web_condenser",
    "--mode", $Mode,
    "--limit", $Limit,
    "--db-path", $DbPath
)

if ($Community) {
    $condenserArgs += "--community"
    $condenserArgs += $Community
}

if ($Verbose) {
    $condenserArgs += "--verbose"
}

if ($ShowThinking) {
    $condenserArgs += "--show-thinking"
}

Write-Host "Mode: $Mode" -ForegroundColor Gray
Write-Host "Limit: $Limit" -ForegroundColor Gray
Write-Host "Database: $DbPath" -ForegroundColor Gray
if ($Verbose) { Write-Host "Verbose: enabled" -ForegroundColor Green }
if ($ShowThinking) { Write-Host "Show thinking: enabled" -ForegroundColor Green }
Write-Host ""

Push-Location $ProjectRoot
try {
    & $Python @condenserArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Web condenser exited with code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Web Condenser Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
