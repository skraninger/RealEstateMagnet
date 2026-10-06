#Requires -Version 5.1
<#
.SYNOPSIS
    Full community data pipeline - discovers and condenses data using all methods

.DESCRIPTION
    Runs every data collection method against every known community, one community
    at a time. Updates the database after each condenser run and persists pipeline
    state so the process can be stopped and restarted at any point.
    
    Condenser order (cheapest/fastest first):
    1. AI Condenser - local model knowledge (no web, fast)
    2. Web Condenser - DuckDuckGo search + local model
    3. Research Agent - deep research with web_search/read_page tools
    4. Browser Condenser - Google search via Chrome + local model
    5. Gemini Condenser - Gemini API with web search grounding (optional)

.PARAMETER Community
    Only process communities matching this name (substring match)

.PARAMETER Condensers
    Comma-separated list of condensers to run (default: all)
    Options: ai, web, research, browser, gemini

.PARAMETER Reset
    Discard existing state and start fresh

.PARAMETER Status
    Print current pipeline status and exit

.PARAMETER StatePath
    Path to state file (default: data/pipeline_state.json)

.PARAMETER Verbose
    Show detailed progress

.EXAMPLE
    .\scripts\run-full-pipeline.ps1
    
    Run the full pipeline on all communities

.EXAMPLE
    .\scripts\run-full-pipeline.ps1 -Community "Pelican Bay"
    
    Run pipeline only for Pelican Bay

.EXAMPLE
    .\scripts\run-full-pipeline.ps1 -Condensers "ai,web"
    
    Run only AI and Web condensers

.EXAMPLE
    .\scripts\run-full-pipeline.ps1 -Status
    
    Show current pipeline status

.EXAMPLE
    .\scripts\run-full-pipeline.ps1 -Reset
    
    Start fresh, discarding previous state

.NOTES
    Requires model server to be running (use start-model-server.ps1)
    Gemini condenser requires API key in .env file
#>

[CmdletBinding()]
param(
    [string]$Community = "",
    [string]$Condensers = "",
    [switch]$Reset,
    [switch]$Status,
    [string]$StatePath = "data/pipeline_state.json",
    [string]$DbPath = "data/communities.db",
    [string]$LogLevel = "INFO"
)

$ErrorActionPreference = "Stop"

# Navigate to project root
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "  RealEstateMagnet - Full Community Data Pipeline" -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host ""

# Check if model server is running
Write-Host "[CHECK] Verifying model server..." -ForegroundColor Yellow
$ModelServerHealthy = $false

# Method 1: Check health endpoint (most reliable)
try {
    $response = Invoke-RestMethod -Uri "http://localhost:8080/health" -TimeoutSec 5 -ErrorAction Stop
    if ($response) {
        Write-Host "[OK] Model server is healthy (health endpoint responding)" -ForegroundColor Green
        $ModelServerHealthy = $true
    }
} catch {
    # Method 2: Check if llama-server process is running
    $llamaProcesses = Get-Process -Name "llama-server" -ErrorAction SilentlyContinue
    if ($llamaProcesses) {
        Write-Host "[OK] Model server process is running (health endpoint not ready yet)" -ForegroundColor Yellow
        Write-Host "     Process ID: $($llamaProcesses.Id), Memory: $([math]::Round($llamaProcesses.WorkingSet64 / 1MB, 2)) MB" -ForegroundColor Gray
        $ModelServerHealthy = $true
    } else {
        Write-Host "[WARNING] Model server is not running" -ForegroundColor Yellow
        Write-Host "  Start it with: .\scripts\start-model-server.ps1" -ForegroundColor Yellow
        Write-Host ""
    }
}

if (-not $ModelServerHealthy) {
    Write-Host ""
    $answer = Read-Host "Model server not running. Continue anyway? (y/N)"
    if ($answer -ne 'y' -and $answer -ne 'Y') {
        Write-Host "Aborted." -ForegroundColor Red
        exit 1
    }
}

# Build command
$PythonCmd = ".venv\Scripts\python.exe"
$ModuleCmd = "modules.community.full_pipeline"

$Args = @()

if ($Community) {
    $Args += "--community"
    $Args += $Community
}

if ($Condensers) {
    $Args += "--condensers"
    $Args += $Condensers
}

if ($Reset) {
    $Args += "--reset"
}

if ($Status) {
    $Args += "--status"
}

$Args += "--state-path"
$Args += $StatePath

$Args += "--db-path"
$Args += $DbPath

$Args += "--log-level"
$Args += $LogLevel

# Run the pipeline
Write-Host "[RUN] Starting full pipeline..." -ForegroundColor Green
Write-Host ""

try {
    & $PythonCmd -m $ModuleCmd @Args
    
    $ExitCode = $LASTEXITCODE
    
    if ($ExitCode -eq 0) {
        Write-Host ""
        Write-Host "======================================================================" -ForegroundColor Green
        Write-Host "  Pipeline completed successfully!" -ForegroundColor Green
        Write-Host "======================================================================" -ForegroundColor Green
    } else {
        Write-Host ""
        Write-Host "======================================================================" -ForegroundColor Red
        Write-Host "  Pipeline failed with exit code $ExitCode" -ForegroundColor Red
        Write-Host "======================================================================" -ForegroundColor Red
        exit $ExitCode
    }
} catch {
    Write-Host ""
    Write-Host "======================================================================" -ForegroundColor Red
    Write-Host "  Pipeline failed with exception: $_" -ForegroundColor Red
    Write-Host "======================================================================" -ForegroundColor Red
    exit 1
}

# Show final summary
if (-not $Status) {
    Write-Host ""
    Write-Host "[SUMMARY] Final database statistics:" -ForegroundColor Cyan
    Write-Host ""
    
    & $PythonCmd -c @"
import sqlite3
import sys
from pathlib import Path

# Database statistics
conn = sqlite3.connect('$DbPath')
cursor = conn.cursor()

cursor.execute('SELECT COUNT(*) FROM communities')
total = cursor.fetchone()[0]

cursor.execute('SELECT COUNT(*) FROM communities WHERE is_gated = 1')
gated = cursor.fetchone()[0]

cursor.execute('SELECT COUNT(DISTINCT community_slug) FROM community_fees')
with_fees = cursor.fetchone()[0]

cursor.execute('SELECT COUNT(DISTINCT community_slug) FROM community_amenities')
with_amenities = cursor.fetchone()[0]

cursor.execute('SELECT COUNT(DISTINCT community_slug) FROM community_demographics')
with_demographics = cursor.fetchone()[0]

print(f'  Total communities: {total}')
print(f'  Gated communities: {gated}')
print(f'  With fee data: {with_fees}')
print(f'  With amenities: {with_amenities}')
print(f'  With demographics: {with_demographics}')

# URL tracking statistics
try:
    cursor.execute('SELECT COUNT(*) FROM source_urls')
    total_urls = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM source_urls WHERE status = ?' , ('printed',))
    printed_urls = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM source_urls WHERE robot_friendly = 1')
    robot_friendly_count = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM source_urls WHERE robot_friendly = 0')
    robot_hostile_count = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM source_urls WHERE status = ?' , ('failed_4xx',))
    failed_4xx = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM source_urls WHERE status = ?' , ('failed_5xx',))
    failed_5xx = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM source_urls WHERE captcha_detected = 1')
    captcha_detected = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM source_urls WHERE captcha_solved = 1')
    captcha_solved = cursor.fetchone()[0]
    
    print('')
    print('  URL Tracking:')
    print(f'    Total URLs tracked: {total_urls}')
    print(f'    Successfully printed: {printed_urls}')
    print(f'    Robot-friendly: {robot_friendly_count} (headless mode)')
    print(f'    Robot-hostile: {robot_hostile_count} (vision browser agent)')
    
    if failed_4xx > 0 or failed_5xx > 0:
        print(f'    Failed (4xx): {failed_4xx}')
        print(f'    Failed (5xx): {failed_5xx}')
    
    if captcha_detected > 0:
        print(f'    CAPTCHAs detected: {captcha_detected}')
        print(f'    CAPTCHAs solved: {captcha_solved}')
        
except sqlite3.OperationalError:
    # source_urls table doesn't exist yet
    pass

conn.close()
"@

    # Show review queue if there are failed URLs
    $ReviewQueueCount = & $PythonCmd -c @"
import sqlite3
from pathlib import Path
try:
    conn = sqlite3.connect('$DbPath')
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM source_urls WHERE status = ? AND reviewed = 0', ('failed_4xx',))
    count = cursor.fetchone()[0]
    conn.close()
    print(count)
except:
    print(0)
"@
    
    if ([int]$ReviewQueueCount -gt 0) {
        Write-Host ""
        Write-Host "[ALERT] $ReviewQueueCount URLs need review (failed with 4xx errors)" -ForegroundColor Yellow
        Write-Host "  Review with: .\scripts\review-urls.ps1 --action review" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "State saved to: $StatePath" -ForegroundColor Gray
Write-Host "To check status: .\scripts\run-full-pipeline.ps1 -Status" -ForegroundColor Gray
Write-Host ""
