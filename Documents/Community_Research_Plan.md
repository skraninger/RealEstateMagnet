# Workstream B — AI Research Assistant (Community Intelligence) — Implementation Plan

Status: IMPLEMENTED (code + docs 2026-10-03; community discovery added 2026-10-03)
Date: 2026-10-03
Baseline: FSD §1 (Primary Objective), §2 (Workstream B deliverables), §3b (Community Master Schema).
Phase 1 + Phase 2 complete; Phase 3 (PostGIS store) is the natural landing zone for this workstream's data.

## 1. Purpose

Compile structured information about **gated communities in southern Florida** so a potential
buyer can determine the best community fit. Per community, capture:

1. **Fees** — HOA dues (monthly/annual), CDD special assessments, other recurring charges.
2. **Amenities** — pool(s), clubhouse, golf, tennis, trails, fitness center, gate/security details, pet policies.
3. **Demographic profile** — median age, median household income, owner-occupancy %, population (Census/ACS preferred).
4. **Proximity to desirable features** — shopping (grocery, malls), public services (libraries, hospitals, schools).

## 2. Method: Research Assistant, Not Scraper

The LLM agent acts as a **research assistant**: it searches the web and reads pages the way a
human buyer's advisor would, then records what it finds into the structured store.

- The agent **chooses** which pages to open based on search results and relevance; it does not
  bulk-crawl, walk sitemaps, or programmatically harvest page sets.
- Each recorded fact carries `source_url`, `retrieved_at`, and a confidence score.
- **robots.txt directives do not apply to this workstream** — the agent browses as a research
  assistant equivalent to a human using a browser. (Workstream A's programmatic crawlers —
  `PoliteClient`/`WebCrawler` — continue to honor robots.txt; the two workstreams are independent.)
- Rate of access should still be reasonable and human-paced; no parallel page-farming.

### Agent tooling (implemented)

| Piece | Choice | Notes |
|-------|--------|-------|
| Model server | **llama.cpp `llama-server`** (Windows host, Vulkan build) | Serves `unsloth/Qwen3.8-27B-GGUF` with multimodal projector (`mmproj-F16.gguf`) for vision capabilities; OpenAI-compatible API at `http://localhost:8080/v1`. Fully local — no cloud LLM. |
| Harness | **PydanticAI 2.x** (`modules/community/agent.py`) | `OpenAIChatModel` pointed at the local endpoint; structured output typed as `CommunityFacts`; agent proposes JSON, code validates before any write. |
| Search | **DuckDuckGo via `ddgs`** (no API key) | `tools.web_search()`; provider errors return empty results so the agent can retry/repurpose queries. |
| Page reading | `httpx` + **trafilatura**, Playwright fallback for JS pages | One request per page, human-paced; text truncated to `RESEARCH_PAGE_MAX_CHARS` (default 8000) before reaching the model. |
| Screenshots | **Playwright** (`tools.take_screenshot()`) | Captures page as PNG for visual inspection when text extraction is insufficient (image-based data, complex layouts). |
| Vision analysis | **Qwen3.8-27B multimodal** via `mmproj-F16.gguf` | `tools.analyze_screenshot()` sends screenshots to the LLM for visual extraction; enables discovery from JS-heavy or image-based pages. |
| Audit | Every tool call → `research_log.jsonl` | search query / URL read / extraction summary + model name. |

### Community Discovery (LLM-powered autonomous list building)

The agent **discovers its own list of communities** using the local LLM to intelligently extract
community names from web pages, rather than relying on regex or a static `target_list.json`.
The discovery workflow:

1. **Search** — Run seed queries like "south florida gated communities", "miami-dade county gated communities"
2. **Read** — Fetch directory/listing pages
3. **Extract (LLM)** — The local model analyzes each page and extracts structured community data (name, city, county, confidence, evidence)
4. **Verify (LLM)** — Each candidate is verified by searching again and having the LLM confirm it's a real gated community in the target area
5. **Output** — Save verified communities to `target_list.json` for research

**Resumable Discovery Process:**

The discovery process is resumable and reports progress in real-time:

```powershell
# Simple one-command discovery (starts server + runs discovery)
.\scripts\run-discovery.ps1

# With verification (slower but more accurate)
.\scripts\run-discovery.ps1 -Verify

# Start fresh (ignore previous state)
.\scripts\run-discovery.ps1 -Reset

# Custom limits
.\scripts\run-discovery.ps1 -MaxCommunities 100 -DeadEndThreshold 5
```

The discovery engine (`modules/community/discovery.py`) uses:
- **Resumable state** — Saves progress to `data/communities/discovery_state.json` after each discovery
- **Progress reporting** — Reports each community as it's discovered via callbacks
- **Stop conditions** — Stops after 200 communities (configurable) or 10 consecutive empty pages (dead end)
- **No duplicate work** — Tracks processed URLs and queries; skips them on resume
- **Multimodal vision** — Falls back to screenshot analysis when text extraction fails
- Same local model as the research agent (Qwen 27B via llama.cpp)

**State File Structure:**

```json
{
  "discovered_communities": [...],
  "verified_communities": [...],
  "processed_urls": [...],
  "processed_queries": [...],
  "consecutive_empty_pages": 0,
  "stopped_reason": null
}
```

When the process stops (reaches max communities or dead end), it records the reason. Run the script again without `-Reset` to continue from where it stopped.

### Visual Extraction (multimodal LLM)

For pages where data is rendered in images, complex layouts, or JavaScript-heavy interfaces that resist text extraction,
the system uses **multimodal vision capabilities** with Qwen3.8-27B:

1. **Automatic fallback** — When text extraction returns insufficient content (< 100 chars), the system automatically:
   - Takes a screenshot of the page using Playwright
   - Sends the image to the LLM for visual analysis
   - Extracts community information from the visual content

2. **Manual vision tools** — The research agent can explicitly use vision when needed:
   - `screenshot_page(url)` — Captures a page as PNG
   - `analyze_image(image_path, question)` — Asks the LLM to analyze the screenshot

3. **Discovery integration** — The community discovery engine uses vision automatically when text extraction fails,
   ensuring communities listed in image-based directories or complex layouts are still found.

```python
from modules.community.tools import take_screenshot, analyze_screenshot

# Take a screenshot of a page
screenshot_path = await take_screenshot("https://example.com/community-info")

# Analyze the screenshot with the multimodal LLM
analysis = await analyze_screenshot(
    screenshot_path,
    "Extract community names, HOA fees, and amenities from this page"
)
```

The vision capability requires the model server to support multimodal input (Qwen3.8-27B with vision encoder).

### Local model serving (setup)

```powershell
# 1. Download prebuilt Windows Vulkan release:
#    https://github.com/ggml-org/llama.cpp/releases -> llama-bXXXX-bin-win-vulkan-x64.zip
#    extract to C:\tools\llama.cpp (must contain llama-server.exe)

# 2. Start the server (first run auto-downloads the GGUF from Hugging Face, ~17 GB for Q4_K_M):
.\scripts\start-model-server.ps1            # defaults: -hf unsloth/Qwen3.8-27B-GGUF, port 8080, ctx 16384

# 3. Verify from the project (host .venv or devcontainer):
python -m modules.community.research_engine --dry-run        # no server needed
python -m modules.community.research_engine --community "Pelican Bay"
```

Tuning: `-GpuLayers` (`-ngl`) controls how many layers go to the Intel Arc B70 (16 GB VRAM);
lower it if VRAM is tight at Q4_K_M, or use a smaller quant. CPU-only fallback works with the
64 GB RAM but is slow (~3–6 tok/s). Server health endpoint: `http://localhost:8080/health`.

**Live test:** one command via `scripts/run-live-test.ps1` (auto-starts the server if down,
researches one community, prints report + artifacts). ⚠️ Do not run it while opencode itself
is served by the local llama-server with the same model — see
`Documents/WorkstreamB_LiveTest_Resume.md` (restart opencode on a non-local model first).

## 3. Scope & Target Geography

- **MVP counties:** Miami-Dade, Broward, Palm Beach (southern Florida).
- **Extension:** Collier, Martin (and any county the buyer adds).
- **Target list seed** (Deliverable B.1): well-known large gated/planned communities to start,
  e.g. The Villages (off-scope geographically — north FL, exclude), Babcock Ranch, Solivita,
  On Top of the World, Pelican Bay, plus agent-discovered gated communities per county.
  Final list is a reviewed catalog file: `data/communities/target_list.json`.

## 4. Source Categories (what the agent reads)

| Category | Examples | Used for |
|----------|----------|----------|
| Community / HOA official sites | Developer or HOA marketing sites, resident portals | Amenities, fees, gate/security details |
| CDD district portals | GMS, Inframark, FDEO registry pages | CDD assessments, fee schedules |
| Real-estate community pages | Realtor.com / Zillow / Trulia "community" pages, local broker sites | Fees (HOA $/mo), amenity lists, price context |
| Census / ACS | data.census.gov, censusreporter.org (5-yr ACS by CDP/place) | Demographics: age, income, owner-occupancy, population |
| Municipal / county sites | Library system branch pages, hospital networks, school district boundary pages | Proximity: nearest library/hospital/school + distances |
| Local news / forums | Community-specific articles, resident discussions | Qualitative fit signals (flag as low-confidence) |

## 5. Structured Store Schema (per FSD §3b)

Lands in the Phase-3 PostGIS database (or JSON files under `data/communities/` until Phase 3
lands — see Milestone M1).

```
communities(id UUID PK, name, slug UNIQUE, county_fips, city, hoa_name, cdd_name,
            is_gated BOOL, geo_point geography(Point,4326), extra JSONB)

community_fees(id, community_id FK, fee_type TEXT,        -- hoa_monthly | hoa_annual | cdd_assessment | other
               amount NUMERIC, period TEXT, currency TEXT DEFAULT 'USD',
               source_url TEXT, retrieved_at TIMESTAMPTZ, confidence NUMERIC)

community_amenities(id, community_id FK, amenity TEXT,    -- normalized key: pool|golf|clubhouse|trails|...
                    detail TEXT,                           -- free text from source
                    source_url TEXT, retrieved_at TIMESTAMPTZ, confidence NUMERIC)

community_demographics(community_id FK PK, median_age NUMERIC,
                       median_household_income NUMERIC, owner_occupancy_pct NUMERIC,
                       population INT, data_year INT,      -- ACS vintage
                       source_url TEXT, retrieved_at TIMESTAMPTZ)

proximity_metrics(id, community_id FK, category TEXT,      -- shopping|grocery|library|hospital|school
                  nearest_name TEXT, distance_miles NUMERIC,
                  source_url TEXT, retrieved_at TIMESTAMPTZ, confidence NUMERIC)

research_log(id, community_id FK NULL, query TEXT, url TEXT, extracted_json JSONB,
             agent TEXT, created_at TIMESTAMPTZ)           -- full audit trail per session
```

**Conflict policy:** never overwrite — keep all sourced facts; when two sources disagree on the
same `fee_type`/amenity, both rows persist and a discrepancy flag is raised in the fit report.
Most-recent `retrieved_at` wins for display only.

## 6. Agent Workflow (Deliverable B.2) — per community

1. **Load** community identity from target list (name, city, county, HOA/CDD names).
2. **Search** rounds: fees ("<community> HOA dues"), amenities, CDD assessments, demographics
   (ACS by place), proximity (nearest library/hospital/school/shopping to community address).
3. **Read** the top relevant results; extract facts into §5 JSON; record every URL consulted in
   `research_log` (including dead ends, for audit).
4. **Validate** extracted JSON against schema (code-side, before any write).
5. **Upsert** facts with provenance; compute discrepancy flags.
6. **Summarize** the community record into the Buyer Fit Report entry (Deliverable B.4):
   fee table, amenity checklist, demographic snapshot, proximity table, open questions.

## 7. Milestones (each independently testable)

Implementation lives in `modules/community/` (`models.py`, `store.py`, `tools.py`,
`agent.py`, `research_engine.py`, `discovery.py`); CLI: `python -m modules.community.research_engine`.

| # | Milestone | Deliverable | Acceptance criteria | Status |
|---|-----------|-------------|---------------------|--------|
| M0 | Community discovery | `discovery.py` — autonomous list building from web search | Can discover and verify communities via CLI; unit tests pass | ✅ (2026-10-03) |
| M1 | Target list + JSON store | `data/communities/target_list.json`, store as validated JSON files + schema | ≥10 seeded southern-FL gated communities with identity fields; JSON schema validation passes | ✅ (10 seeds; Pydantic-validated records) |
| M2 | Extraction schema + validator | Pydantic models for §5 (`models.py`), validation before write | Unit tests: valid/invalid/malformed payloads; provenance fields enforced | ✅ |
| M3 | Research agent loop (1 community) | Agent workflow §6 run end-to-end on one community | All four dimensions populated with ≥1 sourced fact each; `research_log` complete; human review of fit report | ✅ (live run 2026-10-03, 532s, 43 tool calls) |
| M4 | Batch + conflict handling | Run over full target list; discrepancy flags | No silent overwrites; conflicting fee facts both retained and flagged | 🔧 code done (merge + `detect_discrepancies`, unit-tested) — not yet run live |
| M5 | Postgres/PostGIS migration | §5 tables in Phase-3 DB, upserts keyed per fact | Round-trip against devcontainer `db`; marked `@pytest.mark.db` | ⏳ after Phase 3 |
| M6 | Buyer Fit Report generator | Per-community + cross-community comparison (CSV/markdown now; UI later) | Comparison of all researched communities on fees/amenities/demographics/proximity | ⏳ |
| M7 | Visual extraction | Screenshot capability for image-based data | Agent can capture and reference page screenshots when text extraction fails | ✅ (2026-10-03) |

Build order: M0 → M1 → M2 → M3 → M4 → M6 (report first, file-based) → M5 (DB when Phase 3 lands).

## 8. Conventions

- Reuse existing stack: Python 3.12, Pydantic v2 for extraction/storage models, stdlib logging,
  python-dotenv config (`MODEL_BASE_URL`, `MODEL_NAME`, `RESEARCH_MAX_RESULTS`,
  `RESEARCH_PAGE_MAX_CHARS`). New deps in `requirements.txt`: `pydantic-ai`, `ddgs`, `trafilatura`.
  No new HTTP scraping libraries — page reads go through the agent's tools.
- All writes to the store are code-side (agent proposes JSON; code validates + persists) so the
  audit trail and schema guarantees hold regardless of model behavior.
- Tests offline by default: search monkeypatched, HTTP via pytest-httpx, agent via pydantic-ai
  `TestModel` (`tests/test_community.py`, 33 tests); DB tests marked and excluded like Phase 3.

## 9. Risks / Open Questions

1. **Fee data is often stale or marketing-only** — HOA dues on listing sites lag; prefer official
   HOA/CDD sources, mark others low-confidence. Buyer should verify before purchase.
2. **"Gated" definition varies** (single gate vs. multiple entry controls) — record source wording
   in `detail`, keep `is_gated` as confirmed-by-source boolean.
3. **Demographics granularity** — ACS is by place/CDP, not by community; for small communities the
   enclosing census tract is a proxy. Record the geography level used in `extra`.
4. **Proximity measurement** — agent estimates from maps/directions pages; MVP accepts stated or
   approximated distances with source note. Precise geodesic distance is a Phase-3/PostGIS follow-up
   once both community and facility points are in the DB.
5. **Agent cost/time per community** — expect multiple search+read rounds per community; batch runs
   should be resumable (research_log drives "what's already done").

## 10. Out of scope (later phases)

- UI presentation of fit reports (Phase 4), consumer API exposure (Phase 5).
- Parcel-level analysis inside a community (price trends, lot availability) — possible follow-up
  workstream using Workstream A data.
