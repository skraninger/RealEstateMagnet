# Full Community Data Pipeline

## Overview

The Full Pipeline automatically discovers all known communities and processes each through all available data collection methods in sequence. This ensures maximum data coverage by combining:

- **AI Condenser**: Local model knowledge (no web, fastest)
- **Web Condenser**: DuckDuckGo search + local model extraction
- **Research Agent**: Deep research with web search and page reading tools
- **Browser Condenser**: Google search via Chrome + local model
- **Gemini Condenser**: Gemini API with web search grounding (optional, requires API key)

## Key Features

✅ **Automatic Discovery**: Finds communities from target_list.json, discovery_state.json, pipeline_state.json, and database  
✅ **Database-First**: All communities are registered in the database before processing, and every step is persisted there  
✅ **Resumable**: Each community is flagged `processing` → `completed`; a restart begins at the first community not completed  
✅ **URL Attribution**: Every URL inspected for a community is linked to it in the `community_urls` table  
✅ **Error Handling**: Continues processing even if individual condensers fail (the community stays `partial` for retry)  
✅ **Progress Tracking**: Detailed per-community, per-condenser status (queryable from the database)  
✅ **Live Thinking** (on by default): stream the local model's reasoning to the console (disable with `-NoThinking`)

## Quick Start

### Run the full pipeline

```powershell
.\scripts\run-full-pipeline.ps1
```

This will:
1. Discover all communities from all sources and **register them in the database**
2. Begin at the first community not yet `completed`
3. For each community, flag it `processing`, run the remaining condensers in order,
   and link every inspected URL to the community
4. Mark the community `completed` (or `partial` on errors) and continue

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

### See the model thinking (live reasoning)

The model's chain-of-thought is streamed to the console **by default**, indented
under a `🧠 <community> / <condenser> — thinking:` header, as the local model
produces it — so the console shows progress instead of looking frozen.

```powershell
# Default run: thinking is streamed
.\scripts\run-full-pipeline.ps1

# Turn it off for a run (e.g. to keep the log small)
.\scripts\run-full-pipeline.ps1 -NoThinking
```

This requires a reasoning-capable local model: `start-model-server.ps1` launches
llama.cpp with `--reasoning-format deepseek`, which returns thinking in a separate
`reasoning_content` field. Force it on with `start-model-server.ps1 -Reasoning on`
if the model does not think by default.

The run log records the setting as `[CONFIG] ... Thinking=True/False`. On the
Python side it can be disabled with `--no-show-thinking` or by setting
`PIPELINE_SHOW_THINKING=0` in the process environment (`.env` is **not**
auto-loaded by the pipeline). If a step emits no reasoning, the console logs
`(no reasoning emitted by the model for this step)`. Only the local-model
condensers (`ai`, `web`, `research`, `browser`) emit thinking; Gemini is API-side.

To confirm streaming works (or to see thinking without running the pipeline):

```powershell
.venv\Scripts\python.exe scripts\show-model-thinking.py
```

It probes the server for `reasoning_content`, then streams an agent through the
same helper the pipeline uses. Reasoning is forwarded from **every** model turn,
so tool-using condensers (research/browser) show their reasoning during tool
selection as well as the final answer.

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

## Pipeline State (database-first)

The database is the **source of truth** for pipeline progress. `data/pipeline_state.json`
is retained only as a human-readable mirror and one-time migration input.

| Table | Purpose |
|-------|---------|
| `community_pipeline_status` | One row per community: `status`, `sort_order`, `attempts`, `started_at`, `completed_at`, `last_error` |
| `community_condenser_runs` | One row per `(community, condenser)`: `status`, `elapsed`, `fees`, `amenities`, `proximity`, `demographics_present`, `sources_consulted`, `errors` |
| `community_urls` | Many-to-many link of each inspected URL (`source_urls`) to the community it was researched for |

### Community pipeline status values

- `pending`: Registered, not yet processed
- `processing`: Currently being processed (a stale `processing` row resumes on restart)
- `partial`: Some condensers ran but not all selected condensers finished
- `completed`: Every selected condenser finished (`done`, or `skipped` when inapplicable)
- `failed`: Processing aborted with an error recorded in `last_error`

### Condenser run status values

- `pending`, `running`, `done`, `error`, `skipped` (e.g. Gemini without an API key)

### Migration

Run `python migrate_database.py` to create the new tables, register every known
community, assign `sort_order`, backfill `community_urls` from the legacy
`source_urls.community_slug` column, and reconstruct per-condenser progress from
the legacy JSON. It is idempotent and additive (no data is ever dropped) and also
runs automatically on pipeline/viewer startup.

After the first successful run a marker is written to `schema_migrations`, so
subsequent startups skip the migration entirely (a fast no-op). Restarts are
therefore cheap: the pipeline only re-reads community identities and existing
condenser statuses, then resumes at the first community whose status is not
`completed`.

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

The pipeline is community-oriented: it selects the **first community whose
`community_pipeline_status.status` is not `completed`** (ordered by `sort_order`)
and resumes only the condensers that have not yet finished for it.

```powershell
# First run - processes all condensers
.\scripts\run-full-pipeline.ps1

# Pipeline interrupted (Ctrl+C, crash, etc.)

# Second run - resumes at the first community not completed
.\scripts\run-full-pipeline.ps1
```

An interrupted community that was left in `processing` is recovered (marked
`partial`) and its remaining condensers are re-run; condensers already `done` or
`skipped` are not repeated.

To start completely fresh (progress cleared, but **all community data retained**):

```powershell
.\scripts\run-full-pipeline.ps1 -Reset
```

## Community Discovery

Communities are discovered from four sources (in priority order):

1. **target_list.json**: Manually curated list in `data/communities/target_list.json`
2. **discovery_state.json**: Previously discovered communities from web research
3. **Database**: Communities already in `data/communities.db`
4. **pipeline_state.json**: Legacy state (identity only) used for migration

Discovery is deduplicated by slug (normalized name), so "Pelican Bay" won't be
processed more than once. **Every discovered community is registered in the
database** (`communities` + `community_pipeline_status`) before processing begins.

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
│   ├── Load database records
│   └── Load legacy pipeline_state.json (identity only)
│
├── init_state()
│   ├── Merge + dedupe discovered communities
│   ├── register_communities()  -> communities + community_pipeline_status (pending)
│   ├── Optionally reset statuses (--reset; data retained)
│   └── Save the JSON reporting mirror
│
└── run()
    ├── reset_stale_processing()  (interrupted -> partial)
    ├── Select first community whose status != completed (by sort_order)
    ├── For each pending community:
    │   ├── mark_processing()
    │   ├── For each remaining (not done/skipped) condenser:
    │   │   ├── Run condenser (URLs linked via community_urls)
    │   │   ├── record_condenser_run()  -> community_condenser_runs
    │   │   └── Update the JSON reporting mirror
    │   └── mark_completed() if all selected condensers finished, else mark_partial()
    └── Return the reporting mirror
```

## State Persistence

- **Database** (authoritative): every community is registered up front and each
  condenser run is written to `community_condenser_runs` immediately, so a crash
  or Ctrl-C is safe. Community flags live in `community_pipeline_status`.
- **JSON mirror** (`data/pipeline_state.json`): a human-readable copy for
  inspection; it is no longer used to decide what to resume.
- **URL links**: `community_urls` records each URL inspected for a community.

## Future Enhancements

Potential improvements:
- [ ] Parallel condenser execution (currently sequential)
- [ ] Priority-based condenser selection (run only condensers that add value)
- [ ] Automatic retry of failed condensers
- [ ] Progress bar / ETA estimation
- [ ] Export pipeline results to CSV/Markdown
- [ ] Web dashboard for monitoring

## See Also

- `AGENTS.md` - Developer/agent notes (architecture, commands, gotchas)
- `modules/community/full_pipeline.py` - Main pipeline implementation
- `modules/community/database.py` - Schema + all database access
- `modules/community/migration.py` / `migrate_database.py` - Schema migration
- `scripts/run-full-pipeline.ps1` - PowerShell wrapper
- `tests/test_full_pipeline.py`, `tests/test_migration.py` - Test suites
- `data/communities.db` - Source of truth (`community_pipeline_status`,
  `community_condenser_runs`, `community_urls`)
- `data/pipeline_state.json` - Human-readable reporting mirror only
