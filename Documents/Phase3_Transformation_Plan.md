# Phase 3 — Consolidation & Standardization ("The Transformer") — Implementation Plan

Status: DRAFT for review (start here after restart)
Date: 2026-10-02
Baseline: Phase 1 + Phase 2 complete (119/119 tests passing). Raw data lands in
`data/raw/<source-slug>/…` with `data/raw/manifest.json`. `modules/transformation/` is a stub.

## 1. Goals (per FSD)

- **Deliverable 3.1 — Normalization Pipeline:** convert heterogeneous raw records
  (Socrata CSV, ArcGIS attributes, CKAN files, NAL CSVs, scraped HTML tables) into
  the Master Schema (FSD §3): `uuid`, `geo_point`, `source_url`, `standard_score`,
  `last_updated` + canonical domain fields. Reuse Phase-1 `SchemaAnalyzer`
  alias tables (`FIELD_ALIASES`) and confidence scoring.
- **Deliverable 3.2 — Database Schema (PostgreSQL/PostGIS):** relational,
  geospatial-optimized storage with upserts keyed on a deterministic dedup key.

Phase 3 input is **`data/raw` + manifest only** — it must not call the network.

## 2. Architecture

```
modules/transformation/
├── __init__.py            # exports TransformEngine, MasterRecord
├── loaders.py             # manifest-driven raw readers: csv/json/geojson/html-table
│                          #   records → list[dict]; ZIP handling (extract to
│                          #   data/raw/<source>/_extracted/, never mutate originals)
├── normalizer.py          # field mapping (SchemaAnalyzer aliases), type coercion
│                          #   (money, dates, FIPS), FL county/city canonicalization,
│                          #   dedup_key = sha1(county_fips|parcel_id|source)
├── master_schema.py       # MasterRecord dataclass + validation (FSD §3 fields)
├── livability.py          # standard_score 0–100 from sub-metrics: tax rate,
│                          #   flood zone, school rating, crime index, transit access;
│                          #   documented weights; missing metric → neutral fill
├── db/
│   ├── models.py          # SQLAlchemy 2.0 + GeoAlchemy2: property_master,
│   │                      #   sources, ingest_log (raw provenance)
│   └── init_db.py         # idempotent extension/schema creation (postgis, schemas)
└── transform_engine.py    # orchestrator + CLI
                           #   python -m modules.transformation.transform_engine \
                           #     [--source "Broward County Open Data"] [--dry-run]
```

### Key interfaces

- `load_dataset(path, format) -> list[dict]` — one raw file → row dicts (verbatim values).
- `normalize(rows, source) -> list[MasterRecord]` — mapped + coerced; unmapped fields
  preserved in `record.extra` with per-field confidence from Phase 1.
- `score(record, context: CountyContext) -> float` — pure function, unit-testable.
- `TransformEngine.run(source=None, dry_run=False) -> TransformReport`
  (datasets read, rows in/out, mapped/unmapped field counts, records upserted, errors).

### PostGIS schema (MVP)

- `sources(id, name, protocol, base_url, last_ingested_at)`
- `property_master(uuid PK, source_id FK, dedup_key UNIQUE, county_fips, parcel_id,
  owner_name, assessed_value NUMERIC, tax_rate NUMERIC, flood_zone TEXT,
  school_rating NUMERIC, crime_index NUMERIC, transit_score NUMERIC,
  standard_score NUMERIC, geo_point geography(Point,4326) NULL, source_url,
  extra JSONB, last_updated TIMESTAMPTZ)` — upsert on `dedup_key`.
- `ingest_log(id, source_id, dataset_id, manifest_sha256, rows_in, rows_out, created_at)`

## 3. Milestones (each independently testable)

| # | Milestone | Deliverable | Acceptance criteria |
|---|-----------|-------------|---------------------|
| M1 | Raw loaders | `loaders.py` | Reads every manifest format (csv/json/geojson/html-table records, zip extract); fixture files under `tests/fixtures/transformation/`; row counts match Phase-2 manifest |
| M2 | Normalizer | `normalizer.py`, `master_schema.py` | Alias mapping ≥ Phase-1 coverage; money/date/FIPS coercion; county canonicalization for all 67 FL counties (FIPS table); deterministic dedup_key; unmapped → `extra` with confidence |
| M3 | Livability Score | `livability.py` | Pure function; weights documented in docstring; missing metrics neutral; monotonicity property tests |
| M4 | PostGIS schema | `db/models.py`, `db/init_db.py` | Alembic-free idempotent init for MVP; models round-trip against devcontainer `db`; upsert on dedup_key verified |
| M5 | Engine + CLI | `transform_engine.py` | End-to-end: manifest → load → normalize → score → upsert in devcontainer; `--dry-run` reports mapping stats with zero DB access |
| M6 | Test suite | `tests/test_transformation.py` | ≥40 offline unit tests (no DB); DB integration tests behind `@pytest.mark.db` (excluded by default, run in devcontainer) |

Suggested build order: M1 → M2 → M3 → M5(dry-run) → M4 → M6.

## 4. Conventions carried over from Phases 1–2

- Dataclasses for internal types; stdlib `logging` (match `polite_client.py`).
- SQLAlchemy 2.0 + GeoAlchemy2 + asyncpg (already in `requirements.txt`); alembic only if schema churn demands it — MVP uses idempotent `init_db.py`.
- No network in Phase 3; all I/O is `data/raw` + database.
- CLI style matches `modules.ingestion.ingestion_engine` (`--dry-run`, `--log-level`).
- Tests offline by default; DB tests marked and excluded (CI-friendly).

## 5. Test plan

- Fixtures: small CSV/JSON/GeoJSON/HTML-table files under `tests/fixtures/transformation/`
  mirroring real Phase-2 outputs (copy samples from `data/raw/`).
- Property-based checks for score monotonicity and dedup_key stability (hypothesis optional).
- One devcontainer integration test set (`pytest -m db`) against `db:5432`.

## 6. Risks / open questions

1. **Geometry sparsity** — many raw records (tax rolls, NAL) carry no coordinates.
   MVP: `geo_point` nullable; geocoding/parcel-shapefile join is a follow-up task.
2. **Parcel ID formats vary by county** — dedup_key uses normalized
   `county_fips|parcel_id`; cross-source parcel matching (same parcel, different
   format) deferred to Phase 3.x.
3. **PostGIS on host vs devcontainer** — host `.venv` has no local Postgres; DB work
   happens in the devcontainer (`db` service). Unit tests must not require it.
4. **Large NAL files (multi-GB)** — loaders stream CSV via `csv.reader`; memory-safe,
   but batch upserts needed (default 5k rows/batch).
5. **Livability weights** — initial weights are a documented heuristic; refine once
   real county data is loaded (review after first full transform run).

## 7. Out of scope (Phase 4+)

- Search/filter UI, map view, consumer API, provider push API, dedup across counties.
