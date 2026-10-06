# run-browser-condense.ps1 - Browser-driven community data accumulation
# Uses Playwright to automate Chrome/Edge for Google searches with AI gap analysis

param(
    [switch]$Headless,
    [int]$MaxIterations = 20,
    [string]$ChromeProfile = "",
    [switch]$Reset,
    [switch]$Verbose
)

$ErrorActionPreference = "Stop"

# Load environment variables
$envFile = Join-Path $PSScriptRoot "..\.env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match "^\s*([^#][^=]+)=(.*)$" -and $_ -notmatch "^\s*#") {
            $key = $matches[1].Trim()
            $value = $matches[2].Trim()
            if ($value -match "^`"(.*)`"$") { $value = $matches[1] }
            if ($value -match "^'(.*)'$") { $value = $matches[1] }
            [Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
    Write-Host "[OK] Loaded environment from .env" -ForegroundColor Green
} else {
    Write-Host "[WARN] No .env file found - using defaults" -ForegroundColor Yellow
}

# Import modules
$projectRoot = Join-Path $PSScriptRoot ".."
Import-Module (Join-Path $projectRoot "modules\community\__init__.py") -ErrorAction SilentlyContinue

# Build command
$cmd = "python -m modules.community.browser_condenser"

if ($Headless) {
    $cmd += " --headless"
}

if ($MaxIterations -ne 20) {
    $cmd += " --max-iterations $MaxIterations"
}

if ($ChromeProfile) {
    $cmd += " --chrome-profile `"$ChromeProfile`""
}

if ($Reset) {
    $stateFile = Join-Path $projectRoot "data\communities\browser_condenser_state.json"
    if (Test-Path $stateFile) {
        Remove-Item $stateFile -Force
        Write-Host "[OK] Reset state file" -ForegroundColor Green
    }
}

if ($Verbose) {
    $cmd += " --verbose"
}

Write-Host ""
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  Browser Condenser - AI Gap Analysis" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "This will:" -ForegroundColor Yellow
Write-Host "  1. Analyze gaps in current community data"
Write-Host "  2. Generate targeted Google search queries"
Write-Host "  3. Extract community data from search results"
Write-Host "  4. Save results and iterate until gaps are filled"
Write-Host ""
Write-Host "Priority: Data completeness > Geographic coverage"
Write-Host ""
Write-Host "Running: $cmd" -ForegroundColor Gray
Write-Host ""

# Execute
Invoke-Expression $cmd

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "[OK] Browser condenser completed successfully" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "[ERROR] Browser condenser failed with exit code $LASTEXITCODE" -ForegroundColor Red
    exit $LASTEXITCODE
}
