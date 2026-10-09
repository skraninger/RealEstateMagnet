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

.PARAMETER DbPath
    Path to the SQLite database (default: data/communities.db)

.PARAMETER LogLevel
    Python logging level: DEBUG, INFO, WARNING or ERROR (default: INFO)

.PARAMETER LogPath
    Full run log. Empty = logs/run-full-pipeline_<timestamp>.log

.PARAMETER ErrorLogPath
    Focused error report, extracted from the database after the run.
    Empty = logs/pipeline-errors_<timestamp>.log

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
    # Only process communities whose name or slug contains this substring.
    # Empty string = process every discovered community.
    [string]$Community = "",

    # Comma-separated subset of condensers to run, in the fixed order:
    # ai,web,research,browser,gemini. Empty string = all condensers.
    # NOTE: a subset run never marks a community "complete" (completion is
    # always judged against the full set), so it is safe to run one at a time.
    [string]$Condensers = "",

    # Discard recorded progress and start over. Only progress is cleared —
    # community identity and already-collected facts are retained in the DB.
    [switch]$Reset,

    # Print the current pipeline status report (DB-backed) and exit without
    # running any collection.
    [switch]$Status,

    # Stream each condenser's model reasoning ("thinking") to the console while
    # it runs. ON BY DEFAULT. Requires a reasoning-capable model server (see
    # start-model-server.ps1).
    #
    # Kept for backward compatibility; thinking is already on unless -NoThinking.
    [switch]$ShowThinking,

    # Turn the thinking stream off for this run (it is on by default). Useful to
    # keep the log small or when the model's reasoning is very long.
    [switch]$NoThinking,

    # JSON file that mirrors progress for humans and feeds the one-time legacy
    # migration. The SQLite database is the source of truth, NOT this file.
    [string]$StatePath = "data/pipeline_state.json",

    # SQLite database holding community facts and the authoritative pipeline
    # status / condenser-run rows.
    [string]$DbPath = "data/communities.db",

    # Logging level for the Python pipeline module. One of:
    # DEBUG | INFO | WARNING | ERROR. Use DEBUG to capture per-request detail.
    [string]$LogLevel = "INFO",

    # Full run log (key messages + all pipeline output, including Python
    # stderr such as llama.cpp HTTP 400 context-size errors).
    # Empty = logs/run-full-pipeline_<timestamp>.log
    [string]$LogPath = "",

    # Focused error report extracted from the database after the run — this is
    # the file to open first when something failed.
    # Empty = logs/pipeline-errors_<timestamp>.log
    [string]$ErrorLogPath = ""
)

$ErrorActionPreference = "Stop"

# Navigate to project root
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

# ── Console encoding ─────────────────────────────────────────────────────────
# The pipeline logs box-drawing/status glyphs. Force Python into UTF-8 mode and
# match PowerShell's output decoder to it, otherwise the cp1252 Windows console
# makes Python raise UnicodeEncodeError and the run aborts with exit code 1.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# ── Logging setup ────────────────────────────────────────────────────────────
# Every run produces two files under logs/:
#   * a full run log: key messages plus all pipeline output (stdout + stderr,
#     so llama.cpp HTTP 400 context-size errors are captured), and
#   * a focused error report extracted from the database after the run.
# These are what you review when an unattended run fails.
$LogsDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path -LiteralPath $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
}

$RunTimestamp = Get-Date -Format "yyyyMMdd-HHmmss"

if ([string]::IsNullOrWhiteSpace($LogPath)) {
    $LogPath = Join-Path $LogsDir "run-full-pipeline_$RunTimestamp.log"
} elseif (-not [System.IO.Path]::IsPathRooted($LogPath)) {
    $LogPath = Join-Path $ProjectRoot $LogPath
}

if ([string]::IsNullOrWhiteSpace($ErrorLogPath)) {
    $ErrorLogPath = Join-Path $LogsDir "pipeline-errors_$RunTimestamp.log"
} elseif (-not [System.IO.Path]::IsPathRooted($ErrorLogPath)) {
    $ErrorLogPath = Join-Path $ProjectRoot $ErrorLogPath
}

New-Item -ItemType File -Path $LogPath -Force | Out-Null

# Write a line to both the console and the full run log.
function Write-Log {
    param([string]$Message = "", [string]$Color = "White")
    Write-Host $Message -ForegroundColor $Color
    Add-Content -LiteralPath $LogPath -Value $Message -Encoding UTF8
}

# Summarise database errors into the focused report (and the run log).
# Deliberately non-fatal: a reporting failure must never hide the run result.
function Write-RunErrorReport {
    $helper = Join-Path $ProjectRoot "scripts\pipeline_error_report.py"
    if (-not (Test-Path -LiteralPath $helper)) { return }
    if (-not (Test-Path -LiteralPath $PythonCmd)) { return }
    $previousEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $report = & $PythonCmd $helper --db $DbPath --output $ErrorLogPath 2>&1 |
        ForEach-Object { "$_" }
    $ErrorActionPreference = $previousEap
    $report | ForEach-Object { Write-Host $_ }
    Add-Content -LiteralPath $LogPath -Value ($report -join "`r`n") -Encoding UTF8
}

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "  RealEstateMagnet - Full Community Data Pipeline" -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Log "[LOG] Full run log:  $LogPath"
Write-Log "[LOG] Error report:  $ErrorLogPath"
# Thinking stream: on by default; -NoThinking disables, -ShowThinking forces on.
$ThinkingEnabled = $true
if ($NoThinking) {
    $ThinkingEnabled = $false
} elseif ($ShowThinking) {
    $ThinkingEnabled = $true
}

Write-Log "[CONFIG] Community='$Community' Condensers='$Condensers' Reset=$Reset Status=$Status Thinking=$ThinkingEnabled"
Write-Log "[CONFIG] StatePath='$StatePath' DbPath='$DbPath' LogLevel='$LogLevel'"
if (-not $ThinkingEnabled) {
    Write-Log "[CONFIG] Thinking stream is OFF (-NoThinking)"
}

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
        Write-Log "[ABORT] Model server not running - aborted at user request." -Color Red
        exit 1
    }
}

# Build command
$PythonCmd = ".venv\Scripts\python.exe"
$ModuleCmd = "modules.community.full_pipeline"

if (-not (Test-Path -LiteralPath $PythonCmd)) {
    Write-Log "[ABORT] Python interpreter not found at '$PythonCmd' (is the venv set up?)." -Color Red
    Write-RunErrorReport
    exit 1
}

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

if ($ThinkingEnabled) {
    $Args += "--show-thinking"
} else {
    $Args += "--no-show-thinking"
}

$Args += "--state-path"
$Args += $StatePath

$Args += "--db-path"
$Args += $DbPath

$Args += "--log-level"
$Args += $LogLevel

# Run the pipeline
Write-Host "[RUN] Starting full pipeline..." -ForegroundColor Green
Write-Host "  Progress streams below. Press Ctrl+C to stop safely - progress is" -ForegroundColor DarkGray
Write-Host "  saved after every condenser and the run resumes where it left off." -ForegroundColor DarkGray
Write-Host "  Live status from another terminal: .\scripts\run-full-pipeline.ps1 -Status" -ForegroundColor DarkGray
Write-Host ""

try {
    # Stream combined stdout+stderr to the console line-by-line WHILE appending
    # it to the run log. Capturing the output into a variable first would buffer
    # the whole run and show nothing until it finished; Python also block-buffers
    # stdout when it is not a TTY, so we run with -u / PYTHONUNBUFFERED.
    # ErrorActionPreference is relaxed only for this call: merging native stderr
    # with 2>&1 under "Stop" would be treated as a terminating error.
    $PreviousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $PythonCmd -u -m $ModuleCmd @Args 2>&1 | ForEach-Object {
        $line = "$_"
        Write-Host $line
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    }
    $ExitCode = $LASTEXITCODE
    $ErrorActionPreference = $PreviousErrorAction

    if ($ExitCode -eq 0) {
        Write-Log ""
        Write-Log "======================================================================" "Green"
        Write-Log "  Pipeline completed successfully!" "Green"
        Write-Log "======================================================================" "Green"
    } else {
        Write-Log ""
        Write-Log "======================================================================" "Red"
        Write-Log "  Pipeline failed with exit code $ExitCode" "Red"
        Write-Log "======================================================================" "Red"
        Write-RunErrorReport
        Write-Log "[LOG] Review the run log:   $LogPath" "Yellow"
        Write-Log "[LOG] Review the error log: $ErrorLogPath" "Yellow"
        exit $ExitCode
    }
} catch {
    Write-Log ""
    Write-Log "======================================================================" "Red"
    Write-Log "  Pipeline failed with exception: $_" "Red"
    Write-Log "======================================================================" "Red"
    Write-RunErrorReport
    Write-Log "[LOG] Review the run log:   $LogPath" "Yellow"
    Write-Log "[LOG] Review the error log: $ErrorLogPath" "Yellow"
    exit 1
}

# Show final summary
if (-not $Status) {
    Write-Host ""
    Write-Host "[SUMMARY] Final database statistics:" -ForegroundColor Cyan
    Write-Host ""
    
    # Relax ErrorActionPreference only for this reporting call: merging native
    # stderr (2>&1) under "Stop" would turn any diagnostic into a fatal error.
    $PreviousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $SummaryOutput = & $PythonCmd -c @"
import sqlite3
import sys
from pathlib import Path

# Database statistics
conn = sqlite3.connect(sys.argv[1])
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
    
    # Data quality statistics
    try:
        cursor.execute('SELECT COUNT(*) FROM source_urls WHERE has_community_data = 1')
        with_data = cursor.fetchone()[0]
        
        if with_data > 0:
            cursor.execute('SELECT AVG(data_quality_score) FROM source_urls WHERE has_community_data = 1')
            avg_score = cursor.fetchone()[0] or 0
            
            cursor.execute('SELECT MAX(data_quality_score) FROM source_urls WHERE has_community_data = 1')
            max_score = cursor.fetchone()[0] or 0
            
            cursor.execute('SELECT MIN(data_quality_score) FROM source_urls WHERE has_community_data = 1')
            min_score = cursor.fetchone()[0] or 0
            
            # Count URLs by quality tier
            cursor.execute('SELECT COUNT(*) FROM source_urls WHERE data_quality_score >= 75')
            high_quality = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM source_urls WHERE data_quality_score >= 50 AND data_quality_score < 75')
            medium_quality = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM source_urls WHERE data_quality_score > 0 AND data_quality_score < 50')
            low_quality = cursor.fetchone()[0]
            
            print('')
            print('  Data Quality:')
            print(f'    URLs with community data: {with_data}')
            print(f'    Average quality score: {avg_score:.1f}/100')
            print(f'    Score range: {min_score:.0f} - {max_score:.0f}')
            print(f'    High quality (75+): {high_quality}')
            print(f'    Medium quality (50-74): {medium_quality}')
            print(f'    Low quality (<50): {low_quality}')
            
            # Show top 3 highest quality URLs
            cursor.execute('''
                SELECT url, data_quality_score, data_types_found 
                FROM source_urls 
                WHERE has_community_data = 1 
                ORDER BY data_quality_score DESC 
                LIMIT 3
            ''')
            top_urls = cursor.fetchall()
            
            if top_urls:
                print('')
                print('    Top quality sources:')
                for url, score, types in top_urls:
                    # Truncate URL for display
                    display_url = url[:60] + '...' if len(url) > 60 else url
                    print(f'      [{score:.0f}] {display_url}')
                    if types:
                        print(f'           Data: {types}')
    except sqlite3.OperationalError:
        # data quality columns don't exist yet
        pass
        
except sqlite3.OperationalError:
    # source_urls table doesn't exist yet
    pass

# Pipeline status + URL↔community links (new schema)
try:
    cursor.execute('SELECT status, COUNT(*) FROM community_pipeline_status GROUP BY status')
    pipeline_rows = cursor.fetchall()

    print('')
    print('  Pipeline Status:')
    if pipeline_rows:
        for status, count in pipeline_rows:
            print(f'    {status}: {count}')
    else:
        print('    (no statuses recorded yet)')

    cursor.execute('SELECT COUNT(*) FROM community_urls')
    link_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(DISTINCT url_id) FROM community_urls')
    linked_url_count = cursor.fetchone()[0]
    print(f'    URL↔community links: {link_count} ({linked_url_count} unique URLs)')

    cursor.execute('SELECT community_slug, sort_order FROM community_pipeline_status WHERE status != ? ORDER BY sort_order LIMIT 1', ('completed',))
    next_row = cursor.fetchone()
    if next_row:
        print(f'    Next incomplete: {next_row[0]} (order {next_row[1]})')
except sqlite3.OperationalError:
    # new-schema tables don't exist yet
    pass

conn.close()
"@ $DbPath 2>&1 | ForEach-Object { "$_" }
    $ErrorActionPreference = $PreviousErrorAction
    $SummaryOutput | ForEach-Object { Write-Host $_ }
    $SummaryOutput | Out-File -FilePath $LogPath -Append -Encoding utf8

    # Show review queue if there are failed URLs
    $ReviewQueueCount = & $PythonCmd -c @"
import sqlite3
import sys
from pathlib import Path
try:
    conn = sqlite3.connect(sys.argv[1])
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM source_urls WHERE status = ? AND reviewed = 0', ('failed_4xx',))
    count = cursor.fetchone()[0]
    conn.close()
    print(count)
except:
    print(0)
"@ $DbPath
    
    if ([int]$ReviewQueueCount -gt 0) {
        Write-Host ""
        Write-Host "[ALERT] $ReviewQueueCount URLs need review (failed with 4xx errors)" -ForegroundColor Yellow
        Write-Host "  Review with: .\scripts\review-urls.ps1 --action review" -ForegroundColor Yellow
    }
}

# Focused error report — the first file to open when a run had failures.
# (The helper also writes it to $ErrorLogPath.)
Write-RunErrorReport

Write-Log ""
Write-Log "State saved to: $StatePath" "Gray"
Write-Log "To check status: .\scripts\run-full-pipeline.ps1 -Status" "Gray"
Write-Log "[LOG] Full run log:  $LogPath" "Gray"
Write-Log "[LOG] Error report:  $ErrorLogPath" "Gray"
Write-Log ""
