# Full Community Data Pipeline

## Overview

The Full Pipeline automatically discovers all known communities and processes each through all available data collection methods in sequence. This ensures maximum data coverage by combining:

- **AI Condenser**: Local model knowledge (no web, fastest)
- **Web Condenser**: DuckDuckGo search + local model extraction
- **Research Agent**: Deep research with web search and page reading tools
- **Browser Condenser**: Google search via Chrome + local model
- **Gemini Condenser**: Gemini API with web search grounding (optional, requires API key)

## Key Features

✅ **Automatic Discovery**: Finds communities from target_list.json, discovery_state.json, and database  
✅ **Resumable**: Saves state after each step - safe to stop and restart  
✅ **Database Updates**: Persists to SQLite after every condenser completes  
✅ **Error Handling**: Continues processing even if individual condensers fail  
✅ **Progress Tracking**: Detailed per-community, per-condenser status

## Quick Start

### Run the full pipeline

```powershell
.\scripts\run-full-pipeline.ps1
```

This will:
1. Discover all communities from all sources
2. For each community, run all 5 condensers in order
3. Update the database after each condenser
4. Save state after each step (resumable)

### Check pipeline status

```powershell
.\scripts\run-full-pipeline.ps1 -Status
```

### Run for a specific community

```powershell
.\scripts\run-full-pipeline.ps1 -Community "Pelican Bay"
```

### Run specific condensers only

```powershell
# Only AI and Web condensers
.\scripts\run-full-pipeline.ps1 -Condensers "ai,web"

# Only Research Agent
.\scripts\run-full-pipeline.ps1 -Condensers "research"
```

### Start fresh (discard previous state)

```powershell
.\scripts\run-full-pipeline.ps1 -Reset
```

### Python API

```python
from modules.community.full_pipeline import FullPipeline

pipeline = FullPipeline(
    state_path="data/pipeline_state.json",
    database=CommunityDatabase("data/communities.db"),
)

# Run full pipeline
state = await pipeline.run()

# Run for specific community
state = await pipeline.run(community="Pelican Bay")

# Run specific condensers
pipeline = FullPipeline(condensers=["ai", "web"])
state = await pipeline.run()

# Check status
print(f"Processed: {state.stats.successful} condensers")
print(f"Failed: {state.stats.failed} condensers")
```

## Pipeline State

State is saved to `data/pipeline_state.json` after every condenser step. The state file contains:

```json
{
  "version": 1,
  "started_at": "2026-10-05T18:30:00Z",
  "last_updated": "2026-10-05T19:45:00Z",
  "communities": [
    {
      "name": "Pelican Bay",
      "slug": "pelican-bay",
      "city": "Naples",
      "source": "target_list"
    }
  ],
  "results": {
    "pelican-bay": {
      "ai": {
        "status": "done",
        "elapsed": 3.2,
        "fees": 2,
        "amenities": 8,
        "proximity": 3,
        "demographics_present": true
      },
      "web": {
        "status": "done",
        "elapsed": 15.7,
        "fees": 1,
        "amenities": 5
      },
      "research": {
        "status": "pending"
      },
      "browser": {
        "status": "pending"
      },
      "gemini": {
        "status": "skipped",
        "errors": ["API key not configured"]
      }
    }
  },
  "stats": {
    "total_runs": 2,
    "successful": 2,
    "failed": 0,
    "skipped": 1
  }
}
```

### Status Values

- `pending`: Not yet processed
- `running`: Currently being processed
- `done`: Successfully completed
- `error`: Failed (error message in `errors` array)
- `skipped`: Skipped (e.g., Gemini without API key)

## Condenser Order

Condensers run in this order (cheapest/fastest first):

1. **AI Condenser** (~3s per community)
   - Uses local model's pre-trained knowledge
   - No web search required
   - Good baseline data

2. **Web Condenser** (~15s per community)
   - DuckDuckGo search + local model
   - Extracts from real web pages
   - Current, verified data

3. **Research Agent** (~30s per community)
   - Deep research with multiple tool calls
   - Searches, reads pages, analyzes
   - Most thorough data collection

4. **Browser Condenser** (~20s per community)
   - Google search via Chrome automation
   - Access to Google's index
   - Different source than DuckDuckGo

5. **Gemini Condenser** (~10s per community, optional)
   - Google's Gemini API
   - Web search grounding
   - Requires API key (see .env.example)

## Resume Behavior

The pipeline automatically resumes from where it left off:

```powershell
# First run - processes all condensers
.\scripts\run-full-pipeline.ps1

# Pipeline interrupted (Ctrl+C, crash, etc.)

# Second run - resumes from last saved state
.\scripts\run-full-pipeline.ps1
```

To start completely fresh:

```powershell
.\scripts\run-full-pipeline.ps1 -Reset
```

## Community Discovery

Communities are discovered from three sources (in priority order):

1. **target_list.json**: Manually curated list in `data/communities/target_list.json`
2. **discovery_state.json**: Previously discovered communities from web research
3. **Database**: Communities already in `data/communities.db`

Discovery is deduplicated by slug (normalized name), so "Pelican Bay" won't be processed three times.

## Error Handling

- Individual condenser failures don't stop the pipeline
- Errors are logged and recorded in state file
- Pipeline continues with next condenser/community
- Failed condensers can be retried by running pipeline again

Example error in state:

```json
{
  "status": "error",
  "errors": ["Connection timeout after 30s"],
  "elapsed": 30.5
}
```

## Performance

Typical runtimes per community:

| Condenser | Time | Data Quality |
|-----------|------|--------------|
| AI | ~3s | Baseline |
| Web | ~15s | Good |
| Research | ~30s | Excellent |
| Browser | ~20s | Very Good |
| Gemini | ~10s | Good |

**Total per community**: ~80s (all condensers)

**Example**: 100 communities × 5 condensers = ~2.2 hours

## Requirements

- Model server running on `http://localhost:8080`
- SQLite database at `data/communities.db`
- Sufficient disk space for state files

Optional:
- Gemini API key for Gemini condenser (see `.env.example`)

## Testing

```bash
# Run pipeline tests
.venv\Scripts\python.exe -m pytest tests\test_full_pipeline.py -v

# Run all tests
.venv\Scripts\python.exe -m pytest tests\ -v
```

## Troubleshooting

### Pipeline stuck on a community

Check the state file to see which condenser is running:

```powershell
Get-Content data\pipeline_state.json | ConvertFrom-Json | Select-Object -ExpandProperty results
```

### Model server not responding

```powershell
.\scripts\start-model-server.ps1
```

### Gemini condenser skipped

Add API key to `.env`:

```env
MODEL_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
MODEL_NAME=gemini-2.5-flash
MODEL_API_KEY=your-api-key-here
```

### Reset and start over

```powershell
.\scripts\run-full-pipeline.ps1 -Reset
```

### Vision Browser Agent Issues

**Problem:** Browser gets stuck on CAPTCHA

**Solution:**
```powershell
# Check URL tracking stats
.\scripts\review-urls.ps1 -Action stats

# Review failed URLs
.\scripts\review-urls.ps1 -Action review

# The vision browser agent can solve simple CAPTCHAs (Cloudflare, reCAPTCHA checkbox)
# For complex CAPTCHAs, you may need to manually solve them in the browser window
# or mark the URL as blocked
```

**Problem:** Some URLs consistently fail with 403 Forbidden

**Solution:**
```powershell
# Check if the site is robot-friendly
.\scripts\review-urls.ps1 -Action review --category bot_detection

# Robot-friendly sites (government, open data) use fast headless mode
# Non-robot-friendly sites use the vision browser agent
# Some sites may be permanently blocked - mark them as reviewed
.\scripts\review-urls.ps1 -Action mark-reviewed -Url "https://example.com" -Note "Permanently blocked"
```

### Review Queue Management

The pipeline tracks all URLs encountered during research. Failed URLs (especially 4xx errors) go into a review queue:

```powershell
# Show review queue statistics
.\scripts\review-urls.ps1 -Action stats

# View unreviewed failures
.\scripts\review-urls.ps1 -Action review

# View retryable failures (5xx, timeout, network errors)
.\scripts\review-urls.ps1 -Action retryable

# Retry all retryable failures after fixing network issues
.\scripts\review-urls.ps1 -Action retry-all
```

**Failure Categories:**

| Category | Description | Action |
|----------|-------------|--------|
| `cloudflare_block` | Cloudflare challenge | May need manual CAPTCHA solve |
| `bot_detection` | Bot blocked | Try different user agent or proxy |
| `auth_required` | Login required | Provide credentials or skip |
| `not_found` | 404 error | URL no longer exists |
| `rate_limited` | 429 error | Wait and retry |
| `forbidden` | 403 error | May be permanent |
| `timeout` | Request timeout | Network issue, retry |
| `network_error` | DNS/connection failure | Check network, retry |

## Architecture

```
FullPipeline
├── _discover_all_communities()
│   ├── Load target_list.json
│   ├── Load discovery_state.json
│   └── Load database records
│
├── init_state()
│   ├── Create or load PipelineState
│   ├── Initialize results for each community
│   └── Save state to disk
│
└── run()
    ├── For each community:
    │   ├── _ensure_community_record()
    │   └── For each condenser:
    │       ├── Mark as "running"
    │       ├── Run condenser
    │       ├── Update state with result
    │       ├── Save state to disk
    │       └── Update database
    │
    └── Return final state
```

## State Persistence

State is saved to disk after:
- Initialization (community list discovered)
- Each condenser completion
- Each error

This ensures maximum resumability with minimal data loss.

## Future Enhancements

Potential improvements:
- [ ] Parallel condenser execution (currently sequential)
- [ ] Priority-based condenser selection (run only condensers that add value)
- [ ] Automatic retry of failed condensers
- [ ] Progress bar / ETA estimation
- [ ] Export pipeline results to CSV/Markdown
- [ ] Web dashboard for monitoring

## See Also

- `modules/community/full_pipeline.py` - Main pipeline implementation
- `scripts/run-full-pipeline.ps1` - PowerShell wrapper
- `tests/test_full_pipeline.py` - Test suite
- `data/pipeline_state.json` - State file (created on first run)
