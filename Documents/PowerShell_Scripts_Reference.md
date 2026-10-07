# PowerShell Scripts Reference

Complete reference for all PowerShell scripts in the RealEstateMagnet project.

---

## Table of Contents

1. [Infrastructure Scripts](#infrastructure-scripts)
2. [Model Server](#model-server)
3. [Data Collection Scripts](#data-collection-scripts)
4. [Quick Start Guide](#quick-start-guide)
5. [Common Operations](#common-operations)

---

## Infrastructure Scripts

### `Installation/scripts/setup-local-windows.ps1`

**Purpose:** Initial development environment setup for Windows

**Run as Administrator:**
```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
.\Installation\scripts\setup-local-windows.ps1
```

**What it installs:**
- Git for Windows
- WSL 2 (Windows Subsystem for Linux)
- Docker Desktop
- Visual Studio Code
- VS Code extensions:
  - Dev Containers
  - Remote - SSH
  - Python
  - Pylance
  - Black Formatter
  - Docker
  - SQLTools
  - SQLTools PostgreSQL driver
  - GitLens

**Internal Functions:**

| Function | Purpose |
|----------|---------|
| `Write-Step($msg)` | Progress step output (cyan color) |
| `Write-OK($msg)` | Success output (green color) |
| `Write-Warn($msg)` | Warning output (yellow color) |
| `Write-Fail($msg)` | Error output (red color) |
| `Install-WingetPackage($PackageId, $PackageName, $CheckCommand)` | Installs packages via winget if not already present |

**Requirements:**
- Windows 10 21H2+ or Windows 11
- winget (App Installer from Microsoft Store)
- Administrator privileges

---

## Model Server

### `scripts/start-model-server.ps1`

**Purpose:** Starts the local llama.cpp model server with duplicate detection

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `$LlamaDir` | string | `C:\Users\skran\.unsloth\llama.cpp\build\bin\Release` | Directory containing llama-server.exe |
| `$ModelPath` | string | `""` (auto-locate) | Path to .gguf model file |
| `$HfCacheDir` | string | `C:\Users\skran\.cache\huggingface` | Hugging Face cache for model auto-discovery |
| `$ModelAlias` | string | `qwen3.8-27b` | Model name exposed via OpenAI-compatible API |
| `$Port` | int | `8080` | API port |
| `$CtxSize` | int | `32768` | Context window size in tokens |
| `$GpuLayers` | int | `99` | GPU layers to offload (99 = all layers) |
| `$MmprojPath` | string | `""` (auto-locate) | Path to multimodal projector GGUF for vision |
| `$Device` | string | `Vulkan1` | GPU device (use `--list-devices` to see options) |

**Key Features:**

1. **Duplicate Detection** - Two-stage check:
   - Checks health endpoint (`http://localhost:$Port/health`) with 5-second timeout
   - Checks for running `llama-server` processes
   - Warns and exits if server already running or loading

2. **Auto-Discovery** - Automatically finds:
   - Largest Qwen3.8-27B GGUF in Hugging Face cache
   - Multimodal projector (mmproj) file for vision capabilities

3. **Immediate Exit** - Launches server and exits immediately without waiting for model load

**Duplicate Detection Logic:**
```
1. Query health endpoint
   ↓
2. If responding → "Server already running" → exit
   ↓
3. If not responding → Check for llama-server processes
   ↓
4. If processes found → "Server may be loading" → exit
   ↓
5. No processes → Start new server → exit
```

**Examples:**

```powershell
# Start with defaults
.\scripts\start-model-server.ps1

# Custom port and model path
.\scripts\start-model-server.ps1 -Port 8081 -ModelPath "C:\models\custom.gguf"

# Use different GPU device
.\scripts\start-model-server.ps1 -Device "Vulkan0" -GpuLayers 50

# Smaller context window (less VRAM)
.\scripts\start-model-server.ps1 -CtxSize 16384
```

**Output:**
```
Checking for existing model server on port 8080...
  [OK] Server responding on port 8080
  [INFO] Found 1 llama-server process(es): 6096
WARNING: A model server is already running on port 8080!
  - Server is healthy and responding
  - Starting a second instance would waste memory and cause conflicts

Options:
  1. Use the existing server (recommended)
  2. Stop the existing server and restart with different parameters
  3. Use a different port: .\scripts\start-model-server.ps1 -Port <new_port>

To stop the existing server, find and kill the llama-server.exe process:
  Get-Process llama-server | Stop-Process -Force
```

---

## Data Collection Scripts

### `scripts/run-discovery.ps1`

**Purpose:** Discover gated communities via web search and LLM extraction

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `$MaxCommunities` | int | `200` | Maximum number of communities to discover |
| `$DeadEndThreshold` | int | `10` | Stop after N consecutive pages with no new communities |
| `$Verify` | switch | `$false` | Verify each discovered community with LLM (slower but more accurate) |
| `$Reset` | switch | `$false` | Clear state and start fresh |

**Key Features:**

- **Resumable** - Saves state to `data/communities/discovery_state.json` after each discovery
- **Progress reporting** - Shows each community as it's discovered
- **Verification** - Optional LLM verification step for higher confidence
- **Dead end detection** - Stops after N consecutive pages with no new communities
- **Deduplication** - Tracks processed URLs and queries to avoid duplicate work

**State File:** `data/communities/discovery_state.json`

Tracks:
- Discovered communities
- Verified communities
- Processed URLs
- Processed queries
- Consecutive empty pages
- Stop reason

**Examples:**

```powershell
# Basic discovery (200 communities max)
.\scripts\run-discovery.ps1

# With LLM verification
.\scripts\run-discovery.ps1 -Verify

# Limit to 100 communities
.\scripts\run-discovery.ps1 -MaxCommunities 100

# Resume from saved state
.\scripts\run-discovery.ps1

# Start fresh (clear state)
.\scripts\run-discovery.ps1 -Reset
```

---

### `scripts/run-condense.ps1`

**Purpose:** AI-powered data condenser using local model's training knowledge

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `$Mode` | string | `summary` | Operation mode: `discover`, `enrich`, `full`, `summary` |
| `$Limit` | int | `100` | Maximum communities to process |
| `$Community` | string | `""` | Enrich a specific community by name |
| `$Verbose` | switch | `$false` | Show prompts sent to AI, raw responses, and parsed output |
| `$ShowThinking` | switch | `$false` | Show the AI's reasoning process and detailed results |
| `$Reset` | switch | `$false` | Delete existing database and start fresh |

**Key Features:**

- **Fast** - No web searches, uses AI's training knowledge directly
- **Offline capable** - Works without internet connection
- **Multiple modes** - Discover, enrich, or both in sequence
- **Progress monitoring** - Verbose and thinking modes show detailed output
- **Dual storage** - Saves to both file store and SQLite database

**Modes:**

| Mode | Description |
|------|-------------|
| `summary` | Show database statistics (community count, data sources, etc.) |
| `discover` | List communities from AI knowledge with basic info |
| `enrich` | Add detailed facts (fees, amenities, demographics) to known communities |
| `full` | Discover + enrich in one pass |

**Data Source:** `ai_condenser`

**Examples:**

```powershell
# Quick database summary
.\scripts\run-condense.ps1

# Discover 200 communities from AI knowledge
.\scripts\run-condense.ps1 -Mode discover -Limit 200

# Enrich with detailed AI reasoning
.\scripts\run-condense.ps1 -Mode enrich -Limit 50 -ShowThinking

# Full pipeline
.\scripts\run-condense.ps1 -Mode full -Limit 200

# Single community
.\scripts\run-condense.ps1 -Community "Pelican Bay" -Verbose
```

---

### `scripts/run-web-condense.ps1`

**Purpose:** Web-based condenser using DuckDuckGo search + local model

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `$Mode` | string | `summary` | Operation mode: `discover`, `enrich`, `accumulate`, `summary` |
| `$Limit` | int | `50` | Maximum communities to process |
| `$Community` | string | `""` | Enrich a specific community by name |
| `$Verbose` | switch | `$false` | Show prompts sent to local model |
| `$ShowThinking` | switch | `$false` | Show detailed extraction process |
| `$Reset` | switch | `$false` | Delete existing database and start fresh |

**Key Features:**

- **Web search** - Uses DuckDuckGo (free, no API key required)
- **Page reading** - Extracts content from search result pages
- **Local model** - Processes pages with qwen3.8-27b
- **Accumulation loop** - Iteratively discovers and enriches communities
- **Web provenance** - All facts include source URLs

**Modes:**

| Mode | Description |
|------|-------------|
| `summary` | Show database statistics |
| `discover` | Search web for communities and extract basic info |
| `enrich` | Read web pages to add details to known communities |
| `accumulate` | Full discovery + enrichment loop for comprehensive data |

**Data Source:** `web_condenser`

**Examples:**

```powershell
# Database summary
.\scripts\run-web-condense.ps1

# Discover 30 communities from web search
.\scripts\run-web-condense.ps1 -Mode discover -Limit 30

# Full accumulation with verbose output
.\scripts\run-web-condense.ps1 -Mode accumulate -Limit 100 -Verbose

# Research single community with detailed extraction
.\scripts\run-web-condense.ps1 -Community "Fisher Island" -ShowThinking
```

---

### `scripts/run-gemini-condense.ps1`

**Purpose:** High-quality condenser using Google Gemini API with web search grounding

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `$Mode` | string | `summary` | Operation mode: `discover`, `enrich`, `accumulate`, `summary` |
| `$Limit` | int | `50` | Maximum communities to process |
| `$Community` | string | `""` | Enrich a specific community by name |
| `$Verbose` | switch | `$false` | Show prompts and responses |
| `$ShowThinking` | switch | `$false` | Show detailed extraction process and results |
| `$Reset` | switch | `$false` | Delete existing database and start fresh |

**Key Features:**

- **Gemini API** - Uses Google Gemini with web search grounding
- **High quality** - AI reasoning + fresh web information
- **Fast** - 2-5 seconds per query
- **Structured output** - Direct JSON extraction
- **Rate limits** - Free tier: 15 requests/minute, 1500/day

**Configuration Required:**

Create `.env` file:
```env
MODEL_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
MODEL_NAME=gemini-3.8-flash
MODEL_API_KEY=your-gemini-api-key-here
```

Get free API key: https://aistudio.google.com/apikey

**Modes:**

| Mode | Description |
|------|-------------|
| `summary` | Show database statistics |
| `discover` | Gemini searches for communities |
| `enrich` | Gemini researches known communities in detail |
| `accumulate` | Discover + enrich loop for comprehensive data |

**Data Source:** `gemini_condenser`

**Examples:**

```powershell
# Database summary
.\scripts\run-gemini-condense.ps1

# Discover 50 communities
.\scripts\run-gemini-condense.ps1 -Mode discover -Limit 50

# Enrich with detailed output
.\scripts\run-gemini-condense.ps1 -Mode enrich -Limit 20 -ShowThinking

# Full accumulation
.\scripts\run-gemini-condense.ps1 -Mode accumulate -Limit 100 -Verbose

# Single community
.\scripts\run-gemini-condense.ps1 -Community "Pelican Bay" -ShowThinking
```

---

### `scripts/run-browser-condense.ps1`

**Purpose:** Browser-driven condenser with Chrome automation and AI gap analysis

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `$Headless` | switch | `$false` | Run browser without visible UI |
| `$MaxIterations` | int | `20` | Maximum loop iterations |
| `$ChromeProfile` | string | `""` | Path to Chrome profile directory |
| `$Reset` | switch | `$false` | Clear state file |
| `$Verbose` | switch | `$false` | Detailed logging output |

**Key Features:**

- **Google search** - Automates Chrome for Google-quality results
- **AI gap analysis** - Local model identifies missing data
- **Targeted queries** - Generates queries to fill specific gaps
- **Priority system** - Data completeness > geographic coverage
- **Resumable** - Saves state after each iteration
- **JavaScript extraction** - Robust HTML parsing that adapts to structure changes
- **Robot-friendly detection** - Automatically detects robot-friendly sites and uses fast headless mode
- **Vision-driven CAPTCHA handling** - LLM autonomously solves CAPTCHAs on non-robot-friendly sites

**Priority Order:**
1. Missing fees (highest priority)
2. Missing amenities
3. Missing demographics
4. Missing proximity data
5. Geographic gaps (counties with < 3 communities)

**State File:** `data/communities/browser_condenser_state.json`

Tracks:
- Executed queries
- Discovered communities
- Enriched communities
- Total iterations
- Last gap analysis result
- Last query executed
- Stop reason

**Data Source:** `browser_condenser`

**Examples:**

```powershell
# Basic run (20 iterations)
.\scripts\run-browser-condense.ps1

# Extended run with detailed output
.\scripts\run-browser-condense.ps1 -MaxIterations 50 -Verbose

# Headless mode (no browser window)
.\scripts\run-browser-condense.ps1 -Headless -MaxIterations 100

# Start fresh
.\scripts\run-browser-condense.ps1 -Reset

# Use specific Chrome profile
.\scripts\run-browser-condense.ps1 -ChromeProfile "C:\Users\YourName\AppData\Local\Google\Chrome\User Data"
```

**Loop Process:**
```
1. Analyze gaps → What data is missing?
2. Generate query → Create targeted search query
3. Execute search → Chrome searches Google
4. Check robot-friendly → robots.txt + bot-detection probe
5. Route appropriately:
   - Robot-friendly → Fast headless browser (no LLM needed)
   - Non-robot-friendly → Vision browser agent (LLM handles CAPTCHAs)
6. Read pages → Extract content from results
7. Save data → Store in database + URL tracker
8. Repeat → Until max iterations or no gaps
```

**URL Tracking:**

All URLs encountered during browsing are tracked in the `source_urls` table:
- `robot_friendly` field records detection result
- `status` tracks processing state (pending, accessed, printed, failed)
- `pdf_path` cross-references saved PDFs
- Review queue for failed URLs (4xx errors)

---

### `scripts/review-urls.ps1`

**Purpose:** Review and manage tracked URLs, especially failed URLs that need manual attention

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `$Action` | string | `stats` | Action: `stats`, `review`, `retryable`, `mark-reviewed`, `retry`, `retry-all` |
| `$Url` | string | `""` | URL to mark as reviewed or retry |
| `$Note` | string | `""` | Note to add when marking as reviewed |
| `$Limit` | int | `0` | Limit number of results (0 = no limit) |
| `$DbPath` | string | `data/communities.db` | Database path |

**Key Features:**

- **Review queue** - View all unreviewed 4xx failures
- **Failure classification** - See why URLs failed (Cloudflare, bot detection, auth required, etc.)
- **Retry workflow** - Mark URLs as reviewed, retry specific or all retryable failures
- **Statistics** - View URL tracking stats, CAPTCHA solve rates, status counts

**Actions:**

| Action | Description |
|--------|-------------|
| `stats` | Show URL tracking statistics |
| `review` | Show review queue (unreviewed 4xx failures) |
| `retryable` | Show retryable failures (5xx, timeout, network errors) |
| `mark-reviewed` | Mark a URL as reviewed with optional note |
| `retry` | Reset a specific URL to pending for retry |
| `retry-all` | Reset all retryable failures to pending |

**Examples:**

```powershell
# Show URL tracking statistics
.\scripts\review-urls.ps1 -Action stats

# Show review queue (failed URLs)
.\scripts\review-urls.ps1 -Action review

# Show first 20 failed URLs
.\scripts\review-urls.ps1 -Action review -Limit 20

# Show retryable failures
.\scripts\review-urls.ps1 -Action retryable

# Mark URL as reviewed
.\scripts\review-urls.ps1 -Action mark-reviewed -Url "https://example.com/page" -Note "Need login cookies"

# Retry specific URL
.\scripts\review-urls.ps1 -Action retry -Url "https://example.com/timeout"

# Retry all retryable failures
.\scripts\review-urls.ps1 -Action retry-all
```

**Failure Categories:**

| Category | Description | Retryable |
|----------|-------------|-----------|
| `cloudflare_block` | Cloudflare challenge page | No |
| `bot_detection` | Bot detection signal (Access Denied, etc.) | No |
| `auth_required` | Login/authentication required | No |
| `not_found` | 404 Not Found | No |
| `rate_limited` | 429 Too Many Requests | Yes |
| `forbidden` | 403 Forbidden | No |
| `bad_request` | 400 Bad Request | No |
| `geo_blocked` | Geographic restriction | No |
| `timeout` | Request timeout | Yes |
| `network_error` | DNS/connection failure | No |

**URL Processing Flow:**

```
URL discovered
    ↓
Check robots.txt + HTTP probe
    ↓
Robot-friendly?
    ├─ YES → Fast headless browser (no LLM)
    │        ↓
    │        Save PDF + extract text
    │        ↓
    │        Update status to "printed"
    │
    └─ NO  → Vision browser agent (visible browser)
             ↓
             LLM analyzes screenshots
             ↓
             Handle CAPTCHAs autonomously
             ↓
             Save PDF + extract text
             ↓
             Update status to "printed"
             ↓
             (or "failed_4xx" if blocked)
```

**Robot-Friendly Detection:**

The system automatically detects robot-friendly sites by:
1. Checking `robots.txt` for disallow rules
2. Fast HTTP probe for bot-detection signals
3. Caching results at domain level (each domain checked once)

Known robot-friendly domains (pre-cached):
- `.gov` (government sites)
- `.gov.au`, `.gov.uk` (international government)
- `census.gov`, `data.census.gov`
- `arcgis.com`, `opendata.arcgis.com`
- `github.com`, `raw.githubusercontent.com`
- `en.wikipedia.org`

**Benefits:**
- Robot-friendly sites: ~10-20x faster (no LLM calls)
- Non-robot-friendly sites: Full vision agent with CAPTCHA handling
- Domain caching: Each domain only checked once

---

### `scripts/run-live-test.ps1`

**Purpose:** Quick end-to-end test of the research agent on a single community

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `$Community` | string | `Pelican Bay` | Community name to research (substring match) |
| `$SkipStart` | switch | `$false` | Don't auto-start model server |

**Key Features:**

- **Auto-start** - Launches model server if not running
- **Single community** - Fast validation test
- **Artifact inspection** - Shows output files and entry counts
- **Warning system** - Alerts if opencode might conflict

**⚠️ Important Warning:**

Do NOT run this script while opencode itself is being served by the local llama-server with the same model. A second 16 GB instance will contend for VRAM/RAM and can crash the session. Run opencode on a non-local (cloud) model while testing.

**Output Artifacts:**
- `data/communities/communities/<slug>.json` - Community record
- `data/communities/research_log.jsonl` - Audit trail

**Examples:**

```powershell
# Test default community
.\scripts\run-live-test.ps1

# Test specific community
.\scripts\run-live-test.ps1 -Community "Fisher Island"

# Skip auto-start (use existing server)
.\scripts\run-live-test.ps1 -Community "Pelican Bay" -SkipStart
```

---

### `scripts/run-full-pipeline.ps1`

**Purpose:** Execute the complete data collection pipeline using all available condensers in sequence

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `$Community` | string | `""` | Only process communities matching this name (substring match) |
| `$Condensers` | string | `""` | Comma-separated list of condensers to run (default: all) |
| `$Reset` | switch | `$false` | Discard existing state and start fresh |
| `$Status` | switch | `$false` | Print current pipeline status and exit |
| `$StatePath` | string | `data/pipeline_state.json` | Path to state file |
| `$DbPath` | string | `data/communities.db` | Path to database file |
| `$LogLevel` | string | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

**Condensers Available:**

| Condenser | Description | Speed | Cost |
|-----------|-------------|-------|------|
| `ai` | Local AI knowledge (no web) | ⚡ Fastest | Free |
| `web` | DuckDuckGo search + LLM | 🐢 Slow | Free |
| `research` | Deep research with vision browser | 🐢 Slowest | Free |
| `browser` | Google search + Chrome automation | 🐢 Slow | Free |
| `gemini` | Google Gemini API | ⚡ Fast | Free tier limits |

**Key Features:**

- **Sequential execution** - Runs condensers in order: AI → Web → Research → Browser → Gemini
- **URL tracking integration** - Automatically tracks all discovered URLs in database
- **Data quality scoring** - Scores URLs 0-100 based on keyword detection (HOA, amenities, etc.)
- **Robot-friendly detection** - Routes URLs to appropriate extraction method
- **Resumable** - Saves state after each condenser, can resume if interrupted
- **Comprehensive statistics** - Shows URL tracking stats at end of run

**URL Tracking Integration:**

All condensers now integrate with the URL tracking system:
- URLs are registered immediately when discovered
- Quality metrics calculated after page processing
- Status updated to "printed" on success or "failed" on error
- 4xx failures recorded for manual review
- Robot-friendly status cached at domain level

**Data Quality Scoring:**

Each URL is scored 0-100 based on detected keywords:
- **Fees detection** (HOA, dues, assessment, etc.) - up to 25 points
- **Amenities detection** (pool, tennis, golf, etc.) - up to 25 points
- **Demographics detection** (population, income, etc.) - up to 25 points
- **Proximity detection** (shopping, hospital, school, etc.) - up to 25 points

**Output Statistics:**

At the end of each run, the script displays:
- Total communities processed
- Communities with fee/amenity/demographics data
- URL tracking statistics (total, printed, failed, robot-friendly)
- CAPTCHA detection and solve rates
- Data quality metrics (average score, quality tiers, top sources)

**Examples:**

```powershell
# Run full pipeline on all communities
.\scripts\run-full-pipeline.ps1

# Run only specific condensers
.\scripts\run-full-pipeline.ps1 -Condensers "ai,web"

# Run for specific community only
.\scripts\run-full-pipeline.ps1 -Community "Pelican Bay"

# Check pipeline status
.\scripts\run-full-pipeline.ps1 -Status

# Reset and start fresh
.\scripts\run-full-pipeline.ps1 -Reset

# Debug logging
.\scripts\run-full-pipeline.ps1 -LogLevel "DEBUG"
```

**State File:** `data/pipeline_state.json`

Tracks:
- Pipeline version and timestamps
- List of communities to process
- Per-condenser status (pending, running, done, error, skipped)
- Results for each community/condenser combination
- Overall statistics (total runs, successful, failed, skipped)

**Pipeline Flow:**

```
1. Initialize state (discover communities from all sources)
2. For each community:
   a. Run AI condenser (local knowledge)
   b. Run Web condenser (DuckDuckGo + LLM)
   c. Run Research condenser (deep research with vision browser)
   d. Run Browser condenser (Google + Chrome automation)
   e. Run Gemini condenser (Gemini API, if configured)
3. Save state after each condenser
4. Display comprehensive statistics
5. Show review queue if 4xx failures exist
```

**⚠️ Important Notes:**

- Requires model server running (use `start-model-server.ps1`)
- Gemini condenser requires API key in `.env` file
- Pipeline is fully resumable - safe to interrupt and restart
- All URLs are tracked in `source_urls` table with quality scores
- Failed URLs (4xx) require manual review via `review-urls.ps1`

---

## Quick Start Guide

### 1. Initial Setup

```powershell
# Install prerequisites (run as Administrator)
.\Installation\scripts\setup-local-windows.ps1

# Open project in VS Code
code .
# Click "Reopen in Container" when prompted
```

### 2. Start Model Server

```powershell
# Start local model server
.\scripts\start-model-server.ps1

# Check if running
(Invoke-WebRequest -Uri "http://localhost:8080/health" -UseBasicParsing).StatusCode
# Should return: 200
```

### 3. Discover Communities

```powershell
# Method A: LLM-powered discovery (recommended)
.\scripts\run-discovery.ps1 -Verify

# Method B: Quick AI knowledge dump
.\scripts\run-condense.ps1 -Mode discover -Limit 200
```

### 4. Enrich with Data

Choose one condenser based on your needs:

| Script | Quality | Speed | Cost | Best For |
|--------|---------|-------|------|----------|
| `run-condense.ps1` | Medium | ⚡ Fastest | Free | Quick initial seeding |
| `run-web-condense.ps1` | Medium |  Slow | Free | Web-sourced data |
| `run-gemini-condense.ps1` | ⭐ Highest | ⚡ Fast | Free tier limits | High-quality results |
| `run-browser-condense.ps1` | ⭐ High | 🐢 Slowest | Free | Comprehensive coverage |

```powershell
# Example: Use Gemini for best results
.\scripts\run-gemini-condense.ps1 -Mode accumulate -Limit 100 -Verbose
```

### 5. Check Results

```powershell
# View database summary
.\scripts\run-condense.ps1 -Mode summary
```

---

## Common Operations

### Check Model Server Status

```powershell
# Quick health check
try {
    $status = (Invoke-WebRequest -Uri "http://localhost:8080/health" -UseBasicParsing -TimeoutSec 2).StatusCode
    Write-Host "Server status: $status" -ForegroundColor Green
} catch {
    Write-Host "Server not running" -ForegroundColor Red
}
```

### Stop All Model Servers

```powershell
# Kill all llama-server processes
Get-Process llama-server -ErrorAction SilentlyContinue | Stop-Process -Force
Write-Host "All model servers stopped" -ForegroundColor Yellow
```

### View Model Server Processes

```powershell
Get-Process llama-server | Select-Object Id, ProcessName, CPU, @{Name='MemoryMB';Expression={[math]::Round($_.WorkingSet/1MB, 2)}}
```

### Reset All State

```powershell
# Clear discovery state
Remove-Item "data\communities\discovery_state.json" -ErrorAction SilentlyContinue

# Clear browser condenser state
Remove-Item "data\communities\browser_condenser_state.json" -ErrorAction SilentlyContinue

# Clear target list
Remove-Item "data\communities\target_list.json" -ErrorAction SilentlyContinue

# Clear database
Remove-Item "data\communities.db" -ErrorAction SilentlyContinue

Write-Host "All state cleared" -ForegroundColor Yellow
```

### Monitor Database Growth

```powershell
# Watch community count in real-time
while ($true) {
    $output = .\scripts\run-condense.ps1 -Mode summary 2>&1 | Out-String
    $count = ($output | ConvertFrom-Json).total_communities
    Write-Host "$(Get-TimeStamp) Communities: $count" -ForegroundColor Cyan
    Start-Sleep -Seconds 30
}
```

### Export Data to CSV

```python
# Run from Python
from modules.community.database import CommunityDatabase

db = CommunityDatabase("data/communities.db")
db.export_csv("data/exports/communities.csv")
print("Exported to data/exports/communities.csv")
```

### Query Database

```python
# Run from Python
from modules.community.database import CommunityDatabase

db = CommunityDatabase("data/communities.db")

# Find communities with golf amenities
golf_communities = db.query_communities(has_amenity="golf")
print(f"Found {len(golf_communities)} communities with golf")

# Find communities in specific county
miami_communities = db.query_communities(county_fips="12086")
print(f"Found {len(miami_communities)} communities in Miami-Dade")

# Find communities with HOA fees in range
affordable = db.query_communities(max_hoa_monthly=500.0)
print(f"Found {len(affordable)} communities with HOA <= $500/month")
```

---

## Troubleshooting

### Model Server Won't Start

**Problem:** Server exits immediately

**Solution:**
```powershell
# Check if port is in use
netstat -ano | findstr :8080

# Kill existing process
taskkill /PID <PID> /F

# Try different port
.\scripts\start-model-server.ps1 -Port 8081
```

### Discovery Stops Early

**Problem:** Discovery stops after few communities

**Solution:**
```powershell
# Check state file
Get-Content "data\communities\discovery_state.json" | ConvertFrom-Json | Select-Object stopped_reason

# Resume with lower threshold
.\scripts\run-discovery.ps1 -DeadEndThreshold 20
```

### Browser Condenser Gets CAPTCHA

**Problem:** Google shows CAPTCHA during browser automation

**Solution:**
```powershell
# Use headless mode with longer delays
# (Modify browser_condenser.py to increase delay_seconds)

# Or switch to Gemini condenser
.\scripts\run-gemini-condense.ps1 -Mode accumulate
```

### Gemini API Rate Limit

**Problem:** "429 Too Many Requests"

**Solution:**
```powershell
# Wait for quota reset (typically 24 hours)
# Or switch to local model
.\scripts\run-condense.ps1 -Mode accumulate
```

### Database Locked

**Problem:** "database is locked" error

**Solution:**
```powershell
# Close other processes using the database
# Or delete and recreate
Remove-Item "data\communities.db" -Force
```

---

## Script Comparison Matrix

| Script | Web Search | AI Model | Speed | Quality | API Key | Offline |
|--------|-----------|----------|-------|---------|---------|---------|
| `run-discovery.ps1` | ✓ DuckDuckGo | ✓ Local | 🐢 Slow | Medium | No | No |
| `run-condense.ps1` | ✗ None | ✓ Local | ⚡ Fastest | Medium | No | ✓ Yes |
| `run-web-condense.ps1` | ✓ DuckDuckGo | ✓ Local | 🐢 Slow | Medium | No | No |
| `run-gemini-condense.ps1` | ✓ Gemini | ✓ Gemini | ⚡ Fast | ⭐ Highest | ✓ Yes | No |
| `run-browser-condense.ps1` | ✓ Google | ✓ Local | 🐢 Slowest | ⭐ High | No | No |
| `run-live-test.ps1` | ✓ DuckDuckGo | ✓ Local |  Slow | Medium | No | No |

---

## File Locations

| File | Path |
|------|------|
| Discovery state | `data/communities/discovery_state.json` |
| Browser condenser state | `data/communities/browser_condenser_state.json` |
| Target list | `data/communities/target_list.json` |
| Discovered communities | `data/communities/target_list.discovered.json` |
| Community records | `data/communities/communities/*.json` |
| Research log | `data/communities/research_log.jsonl` |
| SQLite database | `data/communities.db` |
| Debug screenshots | `data/google_search_debug.png`, `data/result_container_debug.png` |

---

## Environment Variables

All scripts read from `.env` file in project root:

```env
# Model server configuration
MODEL_BASE_URL=http://localhost:8080/v1
MODEL_NAME=qwen3.8-27b
MODEL_API_KEY=local
MODEL_MAX_TOKENS=8192

# For Gemini (alternative)
# MODEL_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
# MODEL_NAME=gemini-3.8-flash
# MODEL_API_KEY=your-key-here

# Search settings
SEARCH_PROVIDER=duckduckgo
RESEARCH_MAX_RESULTS=8
RESEARCH_PAGE_MAX_CHARS=8000

# Crawler settings
CRAWL_DELAY_SECONDS=1.5
MAX_PAGES_PER_SOURCE=50
CRAWLER_USER_AGENT=RealEstateMagnet/0.1

# Database
DATABASE_URL=sqlite:///data/communities.db

# Logging
LOG_LEVEL=INFO
```

---

## See Also

- [Community Research Plan](../Documents/Community_Research_Plan.md)
- [Data Collection Methods](../Documents/Data_Collection_Methods.md)
- [Functional Specification](../RealEstateMagnet_FSD.md)
- [Workstream B Live Test Guide](../Documents/WorkstreamB_LiveTest_Resume.md)
