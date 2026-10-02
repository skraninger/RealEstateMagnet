# Functional Specification Document (FSD): Real Estate Magnet

## 1. Project Overview
**Goal:** Create a high-performance web platform that aggregates disparate Florida-specific data (Real Estate, Flood Zones, Schools, Taxes, Crime, Transit) and standardizes it into a unified "Decision Engine" for users.

**Target Environment:** Python 3.12 / FastAPI + PostgreSQL 16 / PostGIS 3.4.
**Primary Agent:** Claude Code.
**Development Environment:** Docker devcontainer (VS Code Dev Containers).
**Infrastructure:** Local PC (Docker Desktop) or Proxmox VE 8.x VM/LXC.

---

## 1b. Infrastructure & Development Environment

### Devcontainer (all platforms)
The project uses a **Docker Compose devcontainer** as the canonical development environment. Opening the project in VS Code automatically builds and starts:

| Container | Image | Purpose | Port |
|-----------|-------|---------|------|
| `app` | `python:3.12-slim-bookworm` (custom) | Python dev workspace | — |
| `db` | `postgis/postgis:16-3.4` | PostgreSQL + PostGIS | 5432 |
| `pgadmin` | `dpage/pgadmin4` | Web DB browser | 5050 |

**Quick start (local PC):**
```bash
# 1. Install Docker Desktop + VS Code + Dev Containers extension
.\Installation\scripts\setup-local-windows.ps1   # Windows (run as Admin)

# 2. Open project → VS Code prompts "Reopen in Container"
# 3. Verify:
python -m pytest tests/ -v
```

### Deployment paths

| Path | When to use | Guide |
|------|------------|-------|
| **Local PC** (Docker Desktop) | Daily development, Windows 11 | `Installation/README.md` → Path 1 |
| **Proxmox VM** (Ubuntu 24.04 + Docker) | Persistent dev server, team access | `Installation/README.md` → Path 2 |
| **Proxmox LXC** (Docker-in-container) | Lightweight always-on server | `Installation/README.md` → Path 3 |

### Installation artefacts

```
Installation/
├── README.md                          ← Master guide (all paths)
├── scripts/
│   ├── setup-local-windows.ps1        ← Installs Docker Desktop, VS Code, winget
│   ├── setup-ubuntu-docker.sh         ← Ubuntu: Docker CE, Python, Node, Playwright
│   ├── setup-proxmox-vm.sh            ← Creates Ubuntu 24.04 VM on Proxmox via qm CLI
│   └── init-database.sh               ← PostGIS init: extensions, schemas, base tables
└── proxmox/
    └── README.md                      ← Proxmox sizing, LXC, snapshots, firewall
```

---

## 1c. Project Status (updated 2026-10-02)

| Phase | Status | Where | Verified by |
|-------|--------|-------|-------------|
| 1. Discovery | ✅ Complete | `modules/discovery/` | `tests/test_discovery.py` (60 tests) |
| 2. Ingestion | ✅ Complete | `modules/ingestion/` | `tests/test_ingestion.py` (59 tests) + live smoke run |
| 3. Transformation | ⏳ Next — plan drafted | `Documents/Phase3_Transformation_Plan.md` | — |
| 4. UI | Not started | — | — |
| 5. API | Not started | `modules/api/` (stub) | — |

**Full suite:** `python -m pytest tests/ -q` → **119 passed** (offline; no network needed).

**Phase 2 quick reference (implemented per `Documents/Phase2_Ingestion_Plan.md`):**
```bash
# Harvest a source (discovery + download + raw storage + manifest)
python -m modules.ingestion.ingestion_engine --source "Broward" --limit 5
# Preview without downloading
python -m modules.ingestion.ingestion_engine --source "Orange County" --dry-run
# Re-fetch despite manifest
python -m modules.ingestion.ingestion_engine --source "Broward" --force
```
- Raw output: `data/raw/<source-slug>/<dataset>.<ext>` + `data/raw/manifest.json` (sha256, size, rows, url per fetch). Re-runs skip manifested datasets unless `--force`.
- Single HTTP entry point: `PoliteClient` (per-host rate limit via `CRAWL_DELAY_SECONDS`, tenacity retries w/ `Retry-After`, proxy pool via `PROXY_LIST`).
- Fetchers registered by protocol in `modules/ingestion/fetchers/__init__.py`: `socrata`, `arcgis`, `ckan`, `census`, `direct`, `web` (Playwright branch for `requires_js`; gated sources emit an auth placeholder).
- Playwright 1.63 + chromium installed in host `.venv` (`playwright install chromium`). JS tests skip automatically when the browser is absent.
- Known environment quirk: some hosts (e.g. `opendata.broward.org`, `ocfl.maps.arcgis.com`) fail DNS on the dev machine; those paths are covered by offline fixture tests.

**Next step:** implement Phase 3 following `Documents/Phase3_Transformation_Plan.md`
(raw → Master Schema normalization, PostGIS schema, Livability Score).

---

## 2. Technical Architecture & Deliverables

### Phase 1: Data Search & Discovery (The Intelligence Layer)
* **Deliverable 1.1: Source Discovery Engine:** An automated script to identify and catalog Florida-specific data portals (e.g., Florida Geospatial Data Portal, County Property Appraisers, NOAA). Supports Socrata, ArcGIS REST, CKAN, and direct-download protocols. CLI: `python -m modules.discovery.source_discovery`.
* **Deliverable 1.2: Schema Analyzer:** Tools to inspect JSON, CSV, and HTML structures from discovered sources to map fields to a "Master Florida Schema." Detects GeoJSON geometry, infers field types, and produces a field-mapping report with confidence scores.
* **Deliverable 1.3: Unstructured Web Crawler:** A polite web crawler (`WebCrawler`) for sites that expose no structured API — including gated and community-specific portals. Key capabilities:
  - **Static HTML crawling** (httpx + BeautifulSoup) for government pages, HOA directories, and court record portals.
  - **JS-rendered page support** via Playwright (headless Chromium) for React/Angular portals that hydrate data client-side.
  - **Data-signal detection**: download links (.csv, .xlsx, .zip, .geojson, .shp), HTML data tables, embedded JSON blobs in `<script>` tags, XHR/fetch API patterns, and search forms.
  - **Gated-site access**: optional cookie/session injection for login-gated sources (e.g., MLS systems); `requires_auth=True` sources emit a placeholder with auth instructions if no cookies are provided.
  - **robots.txt compliance**: per-hostname cache; disallowed URLs are recorded but never fetched.
  - **Relevance scoring** (0–1) on each discovered signal based on data format and Florida real-estate keyword density.
  - **Source coverage** includes: FL DBPR HOA registry, county clerk of courts (foreclosure/lis pendens), HOA-USA FL directory, CDD district portals (GMS, Inframark, FDEO registry), SunBiz entity search, large planned communities (The Villages, Babcock Ranch, Solivita, On Top of the World, Pelican Bay), and MLS portals (Stellar MLS, BeachesMLS).
  - CLI: `python -m modules.discovery.web_crawler --url <URL> --depth 2 [--js]`

### Phase 2: Ingestion & Extraction (The Harvester)
* **Deliverable 2.1: Multi-Source Scraper/Downloader:** A robust module using Playwright or BeautifulSoup for unstructured sites, and Axios/Requests for structured APIs.
* **Deliverable 2.2: Rate-Limit & Proxy Manager:** To ensure polite and uninterrupted data gathering from government servers.

### Phase 3: Consolidation & Standardization (The Transformer)
* **Deliverable 3.1: The Normalization Pipeline:** A process that converts "City of Miami" and "MIAMI" into a standard `city_id`.
* **Deliverable 3.2: Database Schema (PostgreSQL/PostGIS):** A relational structure optimized for geospatial queries (e.g., "Find properties within 5 miles of a specific elevation").

### Phase 4: Data Inquiry & Presentation (The UI)
* **Deliverable 4.1: Search & Filter Engine:** A frontend interface allowing users to filter by "Decision Metrics" (e.g., Property Tax < 2%, School Rating > 8, Not in Flood Zone).
* **Deliverable 4.2: Geospatial Map View:** Integration with Mapbox or Leaflet to visualize aggregated data points across the Florida map.

### Phase 5: API Ecosystem (The Interface)
* **Deliverable 5.1: Consumer API:** A REST/GraphQL API for third-party users to query normalized Florida data.
* **Deliverable 5.2: Inbound "Provider" API:** A standardized specification (OpenAPI) that data sources (counties/REOs) can implement to push data directly to the aggregator.

---

## 3. Data Standardization Requirements
Every record must be normalized to the following "Master Schema" before being stored:
| Field            | Type      | Description                                            |
| :--------------- | :-------- | :----------------------------------------------------- |
| `uuid`           | UUID      | Unique identifier across all sources                   |
| `geo_point`      | Geometry  | Lat/Long for mapping                                   |
| `source_url`     | String    | Original data source for traceability                  |
| `standard_score` | Float     | A 0-100 "Livability Score" calculated from sub-metrics |
| `last_updated`   | Timestamp | When the data was last pulled                          |

---

## 4. Implementation Instructions for Claude Code

### Step 0: Environment Setup
> Before any coding steps, ensure the devcontainer is running:
> ```bash
> # Windows — automated prerequisite install (run as Admin):
> Set-ExecutionPolicy Bypass -Scope Process -Force
> .\Installation\scripts\setup-local-windows.ps1
>
> # Then open project in VS Code and click "Reopen in Container"
> # All Python deps, PostGIS, and Playwright are installed automatically.
> ```
> For Proxmox deployment, see `Installation/README.md` → Path 2 or 3.

### Step 1: Initialization
> "Claude, initialize a new project based on this FSD. Create a `/modules` directory for `discovery`, `ingestion`, `transformation`, and `api`."

### Step 2: Discovery Phase
> "Using the specifications in Phase 1, create a discovery script that searches for Florida Open Data portals and outputs a JSON list of available endpoints."

#### Step 2b: Unstructured Source Discovery
> "Run the WebCrawler against HOA directories, CDD portals, and gated community sites to locate downloadable data assets and API patterns not exposed via structured APIs."
>
> ```bash
> # Structured sources only (fast)
> python -m modules.discovery.source_discovery --output data/catalogs/florida_sources.json
>
> # Include unstructured web crawl (slow — uses WebCrawler per web-protocol source)
> python -m modules.discovery.source_discovery --include-unstructured --output data/catalogs/florida_all.json
>
> # Crawl a single URL ad-hoc
> python -m modules.discovery.web_crawler --url https://www.hoa-usa.com/florida/ --depth 3
> ```
>
> For gated sites, pass `web_auth_cookies={"Stellar MLS": {"SMLSSID": "..."}}` to `SourceDiscoveryEngine`.

### Step 3: Database Design
> "Design a Prisma schema for PostgreSQL that reflects the 'Master Schema' in Section 3, including PostGIS support for Florida coordinate systems."

### Step 4: Scraping Logic
> "Create a robust scraper for the Florida Department of Revenue's property tax data, ensuring it maps to our internal 'tax_rate' field."

---

## 5. Success Metrics
* **Accuracy:** 99% match between source data and standardized output.
* **Latency:** API responses under 200ms for geospatial searches.
* **Completeness:** Coverage of at least 15 major Florida counties in the MVP.