# Data Collection Methods Guide

This document describes all the methods available for collecting gated community data in the RealEstateMagnet project. Each method has different strengths, trade-offs, and use cases.

## Overview

The project provides **7 distinct data collection methods** organized into two workstreams:

- **Workstream A**: Programmatic scrapers for structured data sources
- **Workstream B**: AI-powered research assistants for unstructured data

All methods save data to:
- **File store**: `data/communities/communities/*.json`
- **SQLite database**: `data/communities.db`

---

## Method 1: Programmatic Scrapers (Workstream A)

**Module**: `modules/ingestion/`  
**Status**: Implemented

### Description
Automated scrapers for structured data sources like county property appraisers, MLS systems, and government databases.

### Strengths
- Fast and reliable for structured APIs
- Can process large volumes of data
- Deterministic and reproducible

### Limitations
- Limited to available APIs and structured data
- May break if source websites change
- Requires maintenance for each new source

### Use Cases
- County property tax data
- Official government databases
- Structured APIs (Socrata, ArcGIS, CKAN)

### Usage
```bash
python -m modules.ingestion.ingestion_engine --source "Broward" --limit 5
```

---

## Method 2: Community Discovery (Workstream B.0)

**Module**: `modules/community/discovery.py`  
**Script**: `scripts/run-discovery.ps1`  
**Status**: Implemented

### Description
Uses the local LLM to discover communities from web pages. Searches for directory pages, reads them, and extracts community names with structured output.

### Strengths
- Fully automated discovery process
- Resumable (saves state after each discovery)
- Uses local model (no API costs)
- Can verify discovered communities

### Limitations
- Depends on web search quality (DuckDuckGo)
- May miss communities not indexed well
- Slower than API-based methods

### Use Cases
- Initial community list building
- Discovering new communities
- Expanding coverage to new areas

### Usage
```powershell
# Discover communities (default: up to 200)
.\scripts\run-discovery.ps1

# Discover with verification
.\scripts\run-discovery.ps1 -Verify

# Reset and start fresh
.\scripts\run-discovery.ps1 -Reset

# Custom limits
.\scripts\run-discovery.ps1 -MaxCommunities 100 -DeadEndThreshold 5
```

### Output
- `data/communities/discovery_state.json` - Resumable state
- `data/communities/target_list.discovered.json` - Discovered communities

---

## Method 3: Research Agent (Workstream B.2)

**Module**: `modules/community/research_engine.py`  
**Status**: Implemented

### Description
Per-community deep research agent. Searches for and reads multiple pages about a specific community, extracting fees, amenities, demographics, and proximity data.

### Strengths
- Most thorough data collection
- Extracts detailed information per community
- Tracks data provenance (source URLs, retrieval dates)
- Detects discrepancies between sources

### Limitations
- Slow (multiple searches per community)
- Resource-intensive
- Depends on web search quality

### Use Cases
- Detailed research on specific communities
- Filling in missing data for known communities
- Building comprehensive profiles

### Usage
```bash
# Research a single community
python -m modules.community.research_engine --community "Pelican Bay"

# Dry run (list communities without researching)
python -m modules.community.research_engine --dry-run

# Research first N communities
python -m modules.community.research_engine --limit 5
```

### Live Test
```powershell
.\scripts\run-live-test.ps1 -Community "Pelican Bay"
```

---

## Method 4: AI Condenser (Local Model)

**Module**: `modules/community/ai_condenser.py`  
**Script**: `scripts/run-condense.ps1`  
**Status**: Implemented

### Description
Uses the local LLM's knowledge to quickly populate the community database. Asks the AI directly for structured data about communities it already knows about.

### Strengths
- Very fast (no web searches)
- No API costs (uses local model)
- Good for initial data seeding
- Works offline

### Limitations
- Limited to model's training data
- May have outdated information
- Less detailed than web-based methods
- No source URLs (data provenance)

### Use Cases
- Quick initial database population
- Offline data gathering
- Rapid prototyping

### Usage
```powershell
# Discover communities from AI knowledge
.\scripts\run-condense.ps1 -Mode discover -Limit 100

# Enrich known communities
.\scripts\run-condense.ps1 -Mode enrich -Limit 50

# Full pipeline (discover + enrich)
.\scripts\run-condense.ps1 -Mode full -Limit 200

# Show database summary
.\scripts\run-condense.ps1 -Mode summary
```

### Configuration
Uses the local model configured in `.env`:
```
MODEL_BASE_URL=http://localhost:8080/v1
MODEL_NAME=qwen3.8-27b
MODEL_API_KEY=local
```

---

## Method 5: Web Condenser (DuckDuckGo)

**Module**: `modules/community/web_condenser.py`  
**Script**: `scripts/run-web-condense.ps1`  
**Status**: Implemented

### Description
Uses DuckDuckGo web search + local model to accumulate community data from the web. Searches for communities, reads pages, and extracts structured data.

### Strengths
- Better data quality than local model alone
- No API costs (DuckDuckGo is free)
- Web-sourced data with provenance
- Fully automated

### Limitations
- DuckDuckGo results may be less comprehensive than Google
- Slower than API-based methods
- Rate limits on DuckDuckGo

### Use Cases
- Web-based data gathering without API costs
- Supplementing local model knowledge
- Building comprehensive datasets

### Usage
```powershell
# Discover communities from web search
.\scripts\run-web-condense.ps1 -Mode discover -Limit 50

# Enrich communities from web content
.\scripts\run-web-condense.ps1 -Mode enrich -Limit 20

# Full accumulation loop
.\scripts\run-web-condense.ps1 -Mode accumulate -Limit 100 -Verbose
```

---

## Method 6: Gemini Condenser (API)

**Module**: `modules/community/gemini_condenser.py`  
**Script**: `scripts/run-gemini-condense.ps1`  
**Status**: Implemented

### Description
Uses Google Gemini API with web search grounding to accumulate comprehensive community data. Equivalent to using gemini.google.com in a browser but automated.

### Strengths
- Highest quality results
- Web search grounding provides fresh information
- Structured output extraction
- Fast (2-5 seconds per query)

### Limitations
- Requires Gemini API key (free tier: 15 req/min, 1500/day)
- API costs for heavy usage
- Rate limits

### Use Cases
- High-quality data collection
- When accuracy is critical
- Rapid data gathering with good quality

### Usage
```powershell
# Discover communities using Gemini
.\scripts\run-gemini-condense.ps1 -Mode discover -Limit 20

# Enrich a specific community
.\scripts\run-gemini-condense.ps1 -Mode enrich -Community "Pelican Bay" -ShowThinking

# Full accumulation loop
.\scripts\run-gemini-condense.ps1 -Mode accumulate -Limit 100 -Verbose
```

### Configuration
Get a free API key at: https://aistudio.google.com/apikey

```env
MODEL_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
MODEL_NAME=gemini-2.5-flash
MODEL_API_KEY=your-gemini-api-key-here
```

---

## Method 7: Browser Condenser (Chrome + Google)

**Module**: `modules/community/browser_condenser.py`  
**Script**: `scripts/run-browser-condense.ps1`  
**Status**: Implemented

### Description
Uses Playwright to automate Chrome/Edge for Google searches, then uses the local model to extract structured community data from search results. Iteratively analyzes gaps in collected data and generates targeted queries to fill those gaps.

### Strengths
- Google search quality (best results)
- Reuses existing Chrome login (no re-authentication)
- AI-driven gap analysis and query generation
- Priority: data completeness > geographic coverage
- Saves intermediate results (fully resumable)
- No API costs (uses local model + your browser)

### Limitations
- Requires Chrome/Edge installed
- Slower than API-based methods
- May trigger CAPTCHAs on heavy use
- Uses more resources (browser automation)

### Use Cases
- High-quality data collection without API costs
- When you need Google search quality
- Long-running data accumulation
- Comprehensive Florida coverage

### Usage
```powershell
# Basic run (20 iterations)
.\scripts\run-browser-condense.ps1

# Headless mode (no visible browser)
.\scripts\run-browser-condense.ps1 -Headless

# Custom iteration limit
.\scripts\run-browser-condense.ps1 -MaxIterations 50

# Use specific Chrome profile
.\scripts\run-browser-condense.ps1 -ChromeProfile "C:\Users\YourName\AppData\Local\Google\Chrome\User Data"

# Reset state and start fresh
.\scripts\run-browser-condense.ps1 -Reset

# Verbose output
.\scripts\run-browser-condense.ps1 -Verbose
```

### Configuration
```env
# Chrome profile path (reuses existing Chrome login session)
# Windows: %LOCALAPPDATA%\Google\Chrome\User Data
# Linux: ~/.config/google-chrome
# Leave empty to auto-detect or use a fresh profile
CHROME_PROFILE_PATH=

# Browser settings
BROWSER_HEADLESS=false          # Run browser in headless mode
BROWSER_DELAY_SEARCHES=2.0      # Delay between Google searches (seconds)
BROWSER_DELAY_PAGES=1.0         # Delay between page reads (seconds)
BROWSER_MAX_PAGES_PER_QUERY=3   # Max pages to read per search query
BROWSER_MAX_ITERATIONS=20       # Max iterations before stopping
BROWSER_SAVE_INTERVAL=1         # Save state every N iterations
```

### How It Works

1. **Analyze Gaps**: Reviews current data and identifies what's missing
   - Missing fees (highest priority)
   - Missing amenities
   - Missing demographics
   - Missing proximity data
   - Underrepresented counties

2. **Generate Query**: Creates targeted Google search query based on gaps
   - Discovery queries: "gated communities in {county}"
   - Enrichment queries: "{community} HOA fees amenities"
   - Geographic queries: "gated communities {county} Florida"

3. **Execute Search**: Uses Playwright to search Google via Chrome
   - Reuses your existing Chrome profile (already logged in)
   - Extracts search result titles, URLs, snippets

4. **Read & Extract**: Reads top pages and uses local model to extract data
   - Parses page content
   - Extracts structured community data
   - Saves to file store and database

5. **Repeat**: Continues until max iterations or no gaps remain

### State File
`data/communities/browser_condenser_state.json`

Tracks:
- Queries already executed
- Communities discovered
- Current iteration count
- Last gap analysis result
- Stopped reason (if applicable)

---

## Comparison Matrix

| Method | Quality | Speed | Cost | Setup | Best For |
|--------|---------|-------|------|-------|----------|
| **Programmatic Scrapers** | High | Fast | Free | Complex | Structured APIs |
| **Community Discovery** | Medium | Medium | Free | Simple | Initial list building |
| **Research Agent** | High | Slow | Free | Simple | Deep research |
| **AI Condenser (Local)** | Medium | Very Fast | Free | Simple | Quick seeding |
| **Web Condenser (DDG)** | Medium | Medium | Free | Simple | Web data without API |
| **Gemini Condenser** | Very High | Fast | Free/Paid | API key | High-quality results |
| **Browser Condenser** | Very High | Slow | Free | Chrome setup | Comprehensive coverage |

---

## Recommended Workflow

### Phase 1: Initial Data Seeding
1. **AI Condenser** - Quick initial database population from local model knowledge
   ```powershell
   .\scripts\run-condense.ps1 -Mode discover -Limit 200
   ```

2. **Community Discovery** - Expand with web-discovered communities
   ```powershell
   .\scripts\run-discovery.ps1 -Verify
   ```

### Phase 2: Data Enrichment
3. **Gemini Condenser** - High-quality enrichment (if API key available)
   ```powershell
   .\scripts\run-gemini-condense.ps1 -Mode accumulate -Limit 100
   ```

   **OR**

3. **Browser Condenser** - Comprehensive enrichment with Google search
   ```powershell
   .\scripts\run-browser-condense.ps1 -MaxIterations 50
   ```

### Phase 3: Deep Research
4. **Research Agent** - Deep dive on priority communities
   ```bash
   python -m modules.community.research_engine --community "Top Community"
   ```

### Phase 4: Ongoing Maintenance
5. **Programmatic Scrapers** - Regular updates from structured sources
   ```bash
   python -m modules.ingestion.ingestion_engine --source "Broward" --force
   ```

---

## Data Quality & Provenance

All methods track data provenance:
- **source_url**: Where the data came from
- **retrieved_at**: When the data was collected
- **confidence**: Confidence score (0.0-1.0)

Methods that provide source URLs:
- ✓ Programmatic Scrapers
- ✓ Community Discovery
- ✓ Research Agent
- ✗ AI Condenser (local model knowledge, no sources)
- ✓ Web Condenser
- ✓ Gemini Condenser
- ✓ Browser Condenser

---

## Troubleshooting

### Browser Condenser Issues

**Issue**: Browser won't start  
**Solution**: Ensure Chrome/Edge is installed and CHROME_PROFILE_PATH is correct

**Issue**: CAPTCHAs appearing  
**Solution**: Increase BROWSER_DELAY_SEARCHES, reduce BROWSER_MAX_ITERATIONS

**Issue**: No results from Google  
**Solution**: Check if you're logged into Google in Chrome, try a different Chrome profile

### Gemini Condenser Issues

**Issue**: Rate limit exceeded  
**Solution**: Wait for quota reset (typically 24 hours) or use a different model

**Issue**: Model not found  
**Solution**: Check MODEL_NAME in .env (use gemini-2.5-flash or gemini-3.8-flash)

### General Issues

**Issue**: No communities found  
**Solution**: Try different search queries, check if target_list.json has communities

**Issue**: Database locked  
**Solution**: Close other processes using the database, or delete data/communities.db and recreate

---

## Future Enhancements

- **Method 8**: OpenAI condenser (GPT-4o with web search)
- **Method 9**: Anthropic condenser (Claude with web search)
- **Hybrid approach**: Combine multiple methods for best results
- **Conflict resolution**: Automatically resolve discrepancies between sources
- **Data validation**: Cross-reference data across multiple sources

---

## See Also

- [Community Research Plan](Community_Research_Plan.md)
- [Phase 2 Ingestion Plan](Phase2_Ingestion_Plan.md)
- [FSD §2b: Workstream B](../RealEstateMagnet_FSD.md#workstream-b-ai-research-assistant--community-intelligence-the-researcher)
