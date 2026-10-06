<#
.SYNOPSIS
    Runs Gemini-powered community data accumulator with web search grounding.

.DESCRIPTION
    Uses Google Gemini API (gemini-2.5-flash) with web search grounding to
    accumulate comprehensive community data - exactly like using gemini.google.com
    in a browser but automated.

    This provides MUCH better results than DuckDuckGo web search because:
    - Gemini reasons about the data
    - Web search grounding provides fresh information
    - Structured output extraction
    - Much higher quality community data

    Requires a free Gemini API key from: https://aistudio.google.com/apikey

    Configure in .env:
        MODEL_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
        MODEL_NAME=gemini-2.5-flash
        MODEL_API_KEY=your-key-here

.PARAMETER Mode
    Operation mode:
    - discover: Gemini searches for communities (default)
    - enrich: Gemini researches known communities in detail
    - accumulate: Discover + enrich loop for comprehensive data
    - summary: Show database statistics only

.PARAMETER Limit
    Maximum communities to process (default: 50)

.PARAMETER Community
    Enrich a specific community by name

.PARAMETER Verbose
    Show prompts sent to Gemini and extracted data

.PARAMETER ShowThinking
    Show detailed extraction process and results

.PARAMETER Reset
    Delete existing database and start fresh

.EXAMPLE
    .\scripts\run-gemini-condense.ps1
    # Show database summary

.EXAMPLE
    .\scripts\run-gemini-condense.ps1 -Mode discover -Limit 20
    # Discover up to 20 communities using Gemini

.EXAMPLE
    .\scripts\run-gemini-condense.ps1 -Mode enrich -Community "Pelican Bay" -ShowThinking
    # Enrich Pelican Bay with detailed Gemini research

.EXAMPLE
    .\scripts\run-gemini-condense.ps1 -Mode accumulate -Limit 100 -Verbose
    # Full accumulation loop with detailed output
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
Write-Host "  Gemini Community Condenser" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Error "Python venv not found at $Python. Run 'python -m venv .venv' and install requirements first."
}

if ($Reset -and (Test-Path -LiteralPath $DbPath)) {
    Write-Host "[RESET] Removing existing database..." -ForegroundColor Yellow
    Remove-Item -LiteralPath $DbPath -Force
}

# Check for Gemini API key
$envFile = Join-Path $ProjectRoot ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    Write-Warning ".env file not found. Copy from .env.example and add your Gemini API key."
    Write-Warning "Get a free key at: https://aistudio.google.com/apikey"
}

$condenserArgs = @(
    "-m", "modules.community.gemini_condenser",
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
        Write-Error "Gemini condenser exited with code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Gemini Condenser Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
