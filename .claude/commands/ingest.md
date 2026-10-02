Run the Phase 2 ingestion pipeline to download and store raw data from a discovered source.

**Phase 2 IS implemented** (`modules/ingestion/`, complete 2026-10-02). See
`Documents/Phase2_Ingestion_Plan.md` for architecture and completion notes.

## How it works

1. `IngestionEngine` picks a fetcher by protocol from `FETCHER_REGISTRY`
   (`modules/ingestion/fetchers/__init__.py`): socrata, arcgis, ckan, census, direct, web.
2. All HTTP goes through `PoliteClient` (`polite_client.py`): per-host rate limit
   (`CRAWL_DELAY_SECONDS`, default 1.5s), tenacity retries with `Retry-After`,
   optional proxy pool (`PROXY_LIST`) with round-robin rotation on failure.
3. Web sources reuse the Phase-1 `WebCrawler` (Playwright branch for `requires_js`;
   gated sources emit an auth placeholder unless cookies are provided).
4. Output is verbatim bytes under `data/raw/<source-slug>/<dataset>.<ext>` plus a
   `data/raw/manifest.json` entry (url, fetched_at, sha256, format, row_count, size).
   Re-runs skip manifested datasets unless `--force`.

## CLI

```bash
# Harvest a source (name is substring-matched against the source registry)
python -m modules.ingestion.ingestion_engine --source "Broward" --limit 5

# Filter by category / protocol
python -m modules.ingestion.ingestion_engine --source "County" --protocol socrata --category tax

# Preview planned datasets without downloading
python -m modules.ingestion.ingestion_engine --source "Orange County" --dry-run

# Re-fetch despite the manifest
python -m modules.ingestion.ingestion_engine --source "Broward" --force

# Slower/faster politeness, custom output root
python -m modules.ingestion.ingestion_engine --source "Broward" --delay 3 --output data/raw
```

## Notes

- Playwright must be installed for `requires_js` sources:
  `pip install playwright && playwright install chromium` (already done in host `.venv`).
- Some hosts fail DNS on the dev machine (e.g. opendata.broward.org); those paths are
  covered by offline tests — verify with `pytest tests/test_ingestion.py -q`.
- Phase 3 (`modules/transformation/`) consumes `data/raw` + manifest; do not add
  network calls or Master-Schema coupling to ingestion code.
