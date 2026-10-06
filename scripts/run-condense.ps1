<#
.SYNOPSIS
    Runs the AI Condenser to quickly populate the community database.

.DESCRIPTION
    This script uses an AI model to quickly gather structured community data
    and store it in both the file-based store and SQLite database.

    Unlike the research agent (which searches + reads pages per community),
    the condenser asks the AI directly for data it already knows about.
    This is orders of magnitude faster for initial data gathering.

    Supports any OpenAI-compatible API:
    - Local model (llama.cpp): MODEL_BASE_URL=http://localhost:8080/v1
    - Google Gemini: MODEL_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/v1
    - OpenAI: MODEL_BASE_URL=https://api.openai.com/v1

    Set MODEL_API_KEY in .env (use 'local' for llama.cpp).

    Progress monitoring:
    - Use -Verbose to see prompts, responses, and parsed output
    - Use -ShowThinking to see the AI's reasoning process and detailed results

.PARAMETER Mode
    Operation mode:
    - discover: List communities with basic info (default)
    - enrich: Add detailed facts to known communities
    - full: Discover + enrich in one pass
    - summary: Show database statistics only

.PARAMETER Limit
    Maximum communities to process (default: 100)

.PARAMETER Community
    Enrich a specific community by name

.PARAMETER Verbose
    Show prompts sent to AI, raw responses, and parsed output

.PARAMETER ShowThinking
    Show the AI's reasoning process and detailed results for each community

.PARAMETER Reset
    Delete existing database and start fresh

.EXAMPLE
    .\scripts\run-condense.ps1
    # Show database summary

.EXAMPLE
    .\scripts\run-condense.ps1 -Mode discover -Limit 200
    # Discover up to 200 communities from AI knowledge

.EXAMPLE
    .\scripts\run-condense.ps1 -Mode enrich -Limit 50
    # Enrich up to 50 known communities with detailed data

.EXAMPLE
    .\scripts\run-condense.ps1 -Mode full -Limit 200
    # Full pipeline: discover + enrich

.EXAMPLE
    .\scripts\run-condense.ps1 -Community "Pelican Bay"
    # Enrich a single community

.EXAMPLE
    .\scripts\run-condense.ps1 -Mode discover -Limit 5 -Verbose
    # Discover 5 communities with verbose output showing prompts and responses

.EXAMPLE
    .\scripts\run-condense.ps1 -Mode enrich -Limit 3 -ShowThinking
    # Enrich 3 communities showing detailed AI reasoning and extracted data
#>
param(
    [ValidateSet("discover", "enrich", "full", "summary")]
    [string]$Mode = "summary",

    [int]$Limit = 100,

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
Write-Host "  AI Community Condenser" -ForegroundColor Cyan
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
    "-m", "modules.community.ai_condenser",
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
        Write-Error "Condenser exited with code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Condenser Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
