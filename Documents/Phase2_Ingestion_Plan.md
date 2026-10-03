# Phase 2 — Ingestion & Extraction ("The Harvester") — Implementation Plan

Status: COMPLETE (implemented 2026-10-02)
Date: 2026-09-30
Baseline: Phase 1 complete (60/60 tests passing), `modules/ingestion/` is a stub.

## Completion notes (2026-10-02)

- All milestones M1–M6 implemented; full suite **119 passed** (59 new ingestion tests, all offline).
- Playwright 1.63 + chromium v1243 installed in host `.venv`; JS branch verified via a
  `data:` URL render test (skips automatically when the browser is absent).
- Live smoke: `python -m modules.ingestion.ingestion_engine --source "Broward" --limit 2`
  harvested real data from bcpa.net (direct/PDF) and browardclerk.org (web/HTML tables);
  `data/raw/manifest.json` written with sha256/size per fetch.
- Known environment limitation: some hosts (e.g. ocfl.maps.arcgis.com) fail DNS
  resolution on the dev machine — Socrata/ArcGIS live paths are covered by offline
  fixture tests only.
- 2026-10-02 source migration: opendata.broward.org and opendata.coj.net (Socrata)
  are dead (NXDOMAIN). Broward → ArcGIS GeoHub service root (verified live);
  Jacksonville/Duval → data.jacksonville.com via web protocol (bot-blocks automated
  clients; platform unverified). Registry now has zero live Socrata sources.
- httpx 0.28 note: per-request `proxy=` was removed upstream; PoliteClient keeps a
  lazy pool of one `AsyncClient` per proxy (key `None` = no proxy).

## 1. Goals (per FSD)

- **Deliverable 2.1** — Multi-source scraper/downloader: httpx for structured APIs
  (Socrata, ArcGIS REST, CKAN, Census, direct files), BeautifulSoup/Playwright for
  unstructured sites.
- **Deliverable 2.2** — Rate-limit & proxy manager: polite, resumable, uninterrupted
  gathering from government servers.

Phase 2 output is **raw data on disk + a manifest**. Normalization to the Master
Schema is Phase 3 — ingestion must not couple to it.

## 2. Architecture

```
modules/ingestion/
├── __init__.py            # exports IngestionEngine, PoliteClient
├── polite_client.py       # D2.2: per-host rate limiter + proxy manager + retries
│                          #   wraps httpx.AsyncClient; single HTTP entry point
├── fetchers/
│   ├── __init__.py        # FETCHER_REGISTRY: protocol -> fetcher class
│   ├── base.py            # FetchResult dataclass, BaseFetcher ABC
│   ├── socrata_fetcher.py # SODA: dataset list + /export/csv|json (full & paged)
│   ├── arcgis_fetcher.py  # ArcGIS REST: service tree -> feature/imagery layers;
│   │                      #   query for records, export for tiles/shp
│   ├── ckan_fetcher.py    # CKAN API 3: package_list -> resource downloads
│   ├── census_fetcher.py  # Census API (ACS) JSON
│   ├── direct_fetcher.py  # known file URLs (NAL CSV/ZIP, FGDL shapefiles);
│   │                      #   checksum + size validation
│   └── web_fetcher.py     # unstructured: consumes Phase-1 DataSignals;
│                          #   HTML tables -> records, embedded JSON, file links;
│                          #   Playwright path for requires_js sources
├── storage.py             # raw layout data/raw/<source>/<dataset>.<ext> +
│                          #   manifest.json (url, fetched_at, sha256, rows, bytes)
└── ingestion_engine.py    # orchestrator + CLI
                           #   python -m modules.ingestion.ingestion_engine \
                           #     --source "Broward County Open Data" \
                           #     [--category tax] [--protocol socrata] \
                           #     [--limit N] [--output data/raw] [--dry-run]
```

### Key interfaces

- `PoliteClient` (polite_client.py)
  - `async get(url, **kw) -> httpx.Response` — applies per-host min-interval
    (env `CRAWL_DELAY_SECONDS`, default 1.5s), jitter, tenacity retries with
    exponential backoff on 429/5xx, honors `Retry-After`.
  - Proxy manager: optional proxy list from env (`HTTP_PROXY`/`HTTPS_PROXY` or
    `PROXY_LIST` comma-separated); round-robin rotation on connection failure;
    simple health ping; no-proxy fallback.
  - Per-host state in-memory (interval, last-request time, current proxy).

- `FetchResult` (fetchers/base.py)
  - `source_name`, `dataset_id`, `format` (csv|json|geojson|html|zip|shp|pdf),
    `records: list[dict] | None` (for API/table data),
    `raw_bytes: bytes | None` (for file downloads),
    `row_count`, `metadata: dict`, `warnings: list[str]`.

- `IngestionEngine.ingest(source: FloridaSource) -> IngestReport`
  - picks fetcher via `FETCHER_REGISTRY[source.protocol]`,
  - runs with PoliteClient, writes raw files + manifest entry,
  - returns report (datasets fetched, rows, bytes, errors, elapsed).

### Raw storage layout

```
data/raw/
├── manifest.json                 # one entry per fetch: source, dataset, url,
│                                 #   fetched_at, sha256, format, row_count, size
└── <source-slug>/
    └── <dataset-slug>.<ext>      # verbatim bytes from the source
```

- Idempotent re-runs: skip if manifest sha256 matches (or `--force`).
- `.zip`/`.shp` kept unextracted (Phase 3 decides extraction).

## 3. Milestones (each independently testable)

| # | Milestone | Deliverable | Acceptance criteria |
|---|-----------|-------------|---------------------|
| M1 | Polite HTTP layer (D2.2) | `polite_client.py` | Per-host delay enforced (fake-clock test); 429 backs off w/ Retry-After; proxy rotation on failure; all mocked with pytest-httpx |
| M2 | Structured API fetchers | socrata, arcgis, ckan, census fetchers | Each returns FetchResult from fixture payloads; Socrata export pagination; ArcGIS service-tree walk + `where=` query; CKAN resource download |
| M3 | Direct file downloader | `direct_fetcher.py` | Downloads file, records sha256/size; handles ZIP and CSV; 404 -> warning not crash |
| M4 | Web fetcher (D2.1 unstructured) | `web_fetcher.py` | Given Phase-1 `DataSignal`s: extracts HTML tables to records, embedded JSON blobs, download links (delegates files to M3); Playwright branch for `requires_js` (gated sources emit auth-placeholder like Phase 1) |
| M5 | Engine + storage + CLI | `ingestion_engine.py`, `storage.py` | End-to-end run against 1 Socrata + 1 ArcGIS source in devcontainer; manifest written; `--dry-run` lists planned datasets without network |
| M6 | Test suite | `tests/test_ingestion.py` | ≥40 offline tests (fixtures + pytest-httpx); full suite green |

Suggested build order: M1 → M2 → M3 → M5 → M4 (web fetcher last — biggest,
depends on M1/M3; Playwright only if browsers installed).

## 4. Conventions carried over from Phase 1

- `httpx.AsyncClient` + tenacity retries; no new HTTP libraries.
- Dataclasses over pydantic for internal types (matches discovery module).
- structlog or stdlib logging consistent with `web_crawler.py`.
- Env config via python-dotenv: reuse `CRAWL_DELAY_SECONDS`, `CRAWLER_USER_AGENT`;
  add `PROXY_LIST` to `.env.example`.
- CLI style matches `python -m modules.discovery.web_crawler`.

## 5. Test plan

- Offline only (CI-friendly): fixture HTML/JSON/CSV under `tests/fixtures/ingestion/`.
- pytest-httpx for all network; fake sleep/clock for rate-limiter assertions.
- Playwright tests skipped when browsers absent (`pytest.importorskip` / marker).
- One optional live smoke test, excluded by default (`@pytest.mark.live`).

## 6. Risks / open questions

1. **Playwright availability** — not installed in host `.venv`; M4 JS branch is
   skippable until devcontainer/`playwright install chromium`. OK to defer?
2. **NAL bulk files are large** (multi-GB per county for some years). MVP: fetch
   metadata + one small sample file first; add `--max-mb` guard.
3. **ArcGIS export limits** — large feature layers need paged `query` instead of
   `export`; default page size 2000, configurable.
4. **Gated sources (MLS)** — out of scope for MVP; web_fetcher emits the same
   auth-placeholder behavior as Phase 1.
5. Git repo still not initialized — recommend `git init` + first commit before M1.

## 7. Out of scope (Phase 3+)

- Field mapping / canonical normalization, PostGIS schema, dedup, Livability Score.
