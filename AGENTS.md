# AGENTS.md — Developer & Agent Notes

> **Read this first.** This file is the working memory for AI agents (and humans)
> contributing to RealEstateMagnet. It is intentionally practical: architecture,
> commands, invariants, and the gotchas that cost time last session. Keep it up
> to date when you change behaviour — treat it as part of the code contract.
>
> Canonical spec: `RealEstateMagnet_FSD.md`. Pipeline guide:
> `Documents/FULL_PIPELINE_README.md`.

---

## 1. What this project is

Compile structured facts about **gated communities in southern Florida** (fees,
amenities, demographics, proximity) so a buyer can compare communities. Two
acquisition workstreams:

- **Workstream A** — programmatic scrapers/ingestion (`modules/discovery/`,
  `modules/ingestion/`) for structured open-data portals.
- **Workstream B** — AI research/condensing for unstructured data
  (`modules/community/`), which is where most recent work lives.

Stack: Python 3.12, FastAPI, SQLite (dev) → PostgreSQL/PostGIS planned,
Pydantic / PydanticAI, Playwright.

---

## 2. Repository map

```
modules/community/        Workstream B — research, condensers, storage, pipeline
  database.py             SQLAlchemy models + CommunityDatabase (all DB access)
  models.py               Pydantic models (facts, records, condensed items)
  store.py                File-based store (data/communities/communities/*.json)
  full_pipeline.py        DB-first orchestration across all condensers
  migration.py            One-time legacy → pipeline-status schema migration
  url_tracker.py          URL registry + robot-friendly routing + review queue
  agent.py                PydanticAI research agent (web_search/read_page tools)
  research_engine.py      Per-community research orchestrator
  ai_condenser.py         Local-model knowledge (no web)
  web_condenser.py        DuckDuckGo search + local model
  browser_condenser.py    Google via Playwright + local model (gap analysis)
  gemini_condenser.py     Gemini API with web grounding (optional)
  vision_browser_agent.py Playwright vision loop + CAPTCHA handling
modules/web/              FastAPI viewer (viewer.py + templates/)
scripts/                  PowerShell wrappers (run-full-pipeline.ps1, etc.)
tests/                    pytest suite (offline; no network needed)
data/                     Runtime data (tracked in git — see §7)
Documents/                Long-form docs + RESTART_*.md handoff notes
migrate_database.py       Migration entry point
RealEstateMagnet_FSD.md   Functional spec (source of truth for intent)
```

---

## 3. Community pipeline (DB-first) — the important bit

Progress is stored **in the database**, not in a JSON file. The JSON
`data/pipeline_state.json` is now a **human-readable mirror only** (and a
one-time migration input). Filesystem facts still live in
`data/communities/communities/<slug>.json` and are mirrored into SQLite.

### 3.1 Tables (all created by `CommunityDatabase.create_tables()`)

| Table | Role |
|-------|------|
| `communities` | Identity + facts roots. Unique on `slug` (NOT `id`). |
| `community_fees` / `community_amenities` / `community_demographics` / `proximity_metrics` | Facts, one row per fact (conflicts coexist). |
| `community_pipeline_status` | **Resume source of truth.** One row/community: `status`, `sort_order`, `attempts`, `started_at`, `completed_at`, `last_error`. |
| `community_condenser_runs` | One row per `(community, condenser)`: status/results. Replaces JSON `results`. |
| `community_urls` | Many-to-many `community <-> source_urls` link (which URLs were inspected *for* a community). |
| `source_urls` | Canonical URL registry. `url` is UNIQUE. Legacy `community_slug` kept for back-compat. |
| `schema_migrations` | Applied-migration markers (fast startup). |

### 3.2 Status values

- Community (`community_pipeline_status.status`): `pending`, `processing`,
  `partial`, `completed`, `failed`.
- Condenser run (`community_condenser_runs.status`): `pending`, `running`,
  `done`, `error`, `skipped`.
- Helpers in `database.py`: `TERMINAL_OK_STATUSES = ("done", "skipped")`,
  `TERMINAL_STATUSES = ("done", "error", "skipped")`.

### 3.3 Resume semantics

1. `FullPipeline.init_state()` discovers communities from target_list,
   discovery_state, DB identities, and (only when the DB is empty)
   `pipeline_state.json`; then registers them (`register_communities`).
2. `run()` calls `reset_stale_processing()` (a stale `processing` → `partial`),
   then processes communities in `sort_order` whose status is **not
   `completed`** — i.e. it starts at the first unfinished community.
3. For each community: `mark_processing()` → run each selected condenser that
   is **not** already `done`/`skipped` → `record_condenser_run()` per step.
4. Completion uses `self.completion_condensers` (= `ALL_CONDENSERS`), so a
   **subset run (`--condensers ai`) never marks a community complete**. Gemini
   returning `skipped` (not configured) still counts as handled.
5. Errors leave the community `partial` (retried on the next run).

### 3.4 URL attribution

`URLTracker.register_url(url, discovered_by, community_slug=...)` keeps one row
in `source_urls` (UNIQUE by URL) and links it in `community_urls`, so one URL can
belong to several communities. Updates (`update_url_quality`,
`update_url_status`, `update_from_result`) accept `community_slug` and stamp the
link (`inspected`, per-link status/quality). Condensers pass the community slug
where a URL is researched *for* a community; global discovery searches leave it
`None`.

### 3.5 Migration

`migrate_database.py` → `modules/community/migration.py`. Additive/idempotent:
registers all communities, assigns `sort_order`, backfills `community_urls` from
legacy `source_urls.community_slug`, reconstructs condenser runs + derived
statuses from legacy JSON. It then writes a `schema_migrations` marker
(`pipeline_status_v1`) so **later startups are a fast no-op**. It also runs
automatically from `full_pipeline._main` and `modules/web/viewer._ensure_schema`.

**To force a re-migration:** delete the `pipeline_status_v1` row from
`schema_migrations` (or start from a fresh DB). Progress import is additionally
guarded by `count_condenser_runs() == 0`.

---

## 4. Commands

```powershell
# Tests (offline, fast)
.venv\Scripts\python.exe -m pytest tests\ -q

# Full pipeline (needs local model server on :8080)
.\scripts\start-model-server.ps1
.\scripts\run-full-pipeline.ps1                     # resumes at first incomplete
.\scripts\run-full-pipeline.ps1 -Status             # DB-backed status report
.\scripts\run-full-pipeline.ps1 -Condensers "ai,web"
.\scripts\run-full-pipeline.ps1 -Community "Pelican Bay"
.\scripts\run-full-pipeline.ps1 -Reset              # clears progress, keeps data

# Direct module form (prints a harmless runpy RuntimeWarning — see §6)
.venv\Scripts\python.exe -m modules.community.full_pipeline --status

# Migration (idempotent)
.venv\Scripts\python.exe migrate_database.py
.venv\Scripts\python.exe -m modules.community.migration

# Web viewer (runs migration on startup)
.venv\Scripts\python.exe -m modules.web.viewer      # http://localhost:8000

# Focused error report from the DB (also written by the pipeline on failure)
.venv\Scripts\python.exe scripts\pipeline_error_report.py --db data\communities.db
```

Positional args use forward-compatible flags (`--community`, `--condensers`,
`--reset`, `--status`, `--state-path`, `--db-path`, `--log-level`).

---

## 5. Testing conventions

- All tests are **offline** — no network, no model server.
- Pipeline tests use a **real** `CommunityDatabase(tmp_path/...)`, not
  `MagicMock`, because the pipeline is now DB-driven. Helper: `make_db()` in
  `tests/test_full_pipeline.py`.
- `_discover_all_communities` uses `database.list_community_infos()` (lightweight);
  tests mock that, not `list_records`.
- Migration tests must isolate from real data: pass `store_root=` and
  `discovery_state_path=` pointing at empty temp locations
  (`_isolated_kwargs` in `tests/test_migration.py`). Otherwise the migration
  reads the real `data/communities` store and registers ~204 communities.
- URL multi-community linking is covered in
  `tests/test_url_tracker.py::TestCommunityURLLinks`.
- Current status: **297 passing**.

---

## 6. Notes to self (gotchas that cost time)

- **`pipeline_state.json` is NOT authoritative.** Never base resume decisions on
  it. The DB is the source of truth. It is written once per `run()` (not per
  step) for inspection only.
- **`-m modules.community.full_pipeline` prints `RuntimeWarning: ... found in
  sys.modules ...`.** Cause: `modules/community/__init__.py` imports
  `full_pipeline`, so runpy sees it already imported. Harmless. It also makes
  PowerShell report exit code 1 while exiting 0 — don't treat as failure.
- **Unicode crash on Windows consoles (exit code 1).** The pipeline logs
  box-drawing glyphs (`─`) and status icons (`▶ ✓ ✗`); a cp1252 console makes
  `print()` raise `UnicodeEncodeError` and abort the run. `full_pipeline._main`
  calls `_force_utf8_stdio()` (stdout/stderr → UTF-8, `errors="replace"`), and
  `run-full-pipeline.ps1` sets `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8` /
  `[Console]::OutputEncoding`. Keep both; the Python fix covers direct `-m`
  runs, the script fix keeps the captured log readable.
- **PowerShell here-strings in scripts are double-quoted (`@" ... "@`).** Avoid
  `$` inside the embedded Python (it will be interpolated). The pipeline summary
  Python block must not contain `$`.
- **Adding a condenser requires updates in several places:**
  `ALL_CONDENSERS`, `_CONDENSER_RUNNERS`, `_CONDENSER_LABELS` in
  `full_pipeline.py`, and `DEFAULT_CONDENSERS` in `migration.py`. Completion
  automatically follows `ALL_CONDENSERS`.
- **`completion_condensers` is the full set**, deliberately decoupled from the
  run's `--condensers` selection (§3.3 step 4). Don't "optimise" it to the
  selected subset.
- **FKs are not enforced by SQLite by default.** `community_urls` can reference
  a slug not yet in `communities`; the pipeline registers communities first, but
  standalone `URLTracker` use may create links for unknown slugs.
- **`url` is UNIQUE in `source_urls`.** Multi-community attribution must go
  through `community_urls`; do not try to store multiple communities on the row.
- **runpy and CRLF warnings** appear on Windows; they are benign.
- **Runtime data is tracked in git** (see §7) — `git add -A` is normal for data
  changes here, and captcha artifacts/community JSON are expected.
- **Environment quirk:** some hosts (e.g. `ocfl.maps.arcgis.com`) fail DNS on
  the dev machine; covered by offline fixtures.
- **Never commit `.env`** (gitignored); use `.env.example`.
- **Model server context is `-CtxSize 65536`** (raised from 32768). Research
  prompts exceeded the old window and llama.cpp returned HTTP 400
  `exceed_context_size_error`. The Qwen3.8-27B GGUF advertises a native context
  of 262144 tokens, so raise it further if VRAM allows (lower `-GpuLayers` if it
  fails to load). The server's own log is `logs/llama-server.log` (gitignored).
- **Pipeline errors are logged.** `run-full-pipeline.ps1` streams the pipeline
  output live to the console while appending a full run log
  (`logs/run-full-pipeline_<timestamp>.log`, stdout+stderr) and a focused error
  report (`logs/pipeline-errors_<timestamp>.log`, produced by
  `scripts/pipeline_error_report.py` from `community_condenser_runs` +
  `community_pipeline_status`). Review the error report first when a run fails.
- **Progress heartbeat.** Each condenser runs as a task and the pipeline logs
  `⏳ still working on <community> / <condenser> (Ns elapsed)` every 30s
  (`PIPELINE_HEARTBEAT_SECONDS`, `0` disables). The run is resumable, so
  `Ctrl+C` during a long step is safe. (`-LogLevel WARNING` gives a cleaner
  console: progress + heartbeats only.)
- **PowerShell 5.1 quirk:** `Tee-Object` writes UTF-16, so the pipeline script
  streams output through `ForEach-Object { Write-Host $_; Add-Content -Encoding UTF8 }`
  instead. Merging native stderr (`2>&1`) under `$ErrorActionPreference = "Stop"`
  is a terminating error, so the script relaxes it around those calls.
- **`read_page` only opens a visible browser when `use_js=True`.** Its cascade is
  static → headless JS render → (opt-in) visible Chrome PDF capture. The visible
  fallback keeps **one** browser session and, on a CAPTCHA, saves a `.png`
  screenshot and polls the same window for `CAPTCHA_MANUAL_WAIT_SECONDS` (default
  60) — it never relaunches fresh browsers. Do not reintroduce a fresh-browser
  retry loop: it opened the page 3× and could not carry a solved challenge.
  Robot-hostile URLs should go through the vision agent
  (`URLTracker.process_url` → `access_and_extract`).
- **Performance invariants (keep them!):** no per-step JSON-mirror writes;
  migration is marker-gated; `register_communities` is batched;
  `list_pipeline_statuses` and `condenser_statuses_map` are single queries;
  don't reintroduce per-community queries in `run()`.

---

## 7. Data & git

- Tracked: `data/communities.db`, `data/communities/communities/*.json`,
  `data/pipeline_state.json`, `data/captcha_artifacts/*` (runtime artifacts are
  committed in this repo).
- Ignored: `.env*`, `data/raw/`, `data/catalogs/*.json`, `data/downloads/`,
  `data/exports/`, `*.log`, virtualenvs, caches.
- Current real-DB snapshot (after migration): 204 communities, 7 `completed`,
  4 `partial`, 193 `pending`; 50 condenser runs; 4 `community_urls` links;
  595 `source_urls`.

---

## 8. Related docs

- `RealEstateMagnet_FSD.md` — functional spec, §3b/§3c schema + pipeline model.
- `Documents/FULL_PIPELINE_README.md` — pipeline usage, state, resume.
- `Documents/Data_Collection_Methods.md` — the 7 collection methods.
- `Documents/Community_Research_Plan.md` — Workstream B design.
- `Documents/RESTART_*.md` — dated handoff notes (session history).
- `Documents/WEB_VIEWER.md` — viewer guide.

When you finish a work session, add a short `Documents/RESTART_<date>.md` and
update this file if any invariant/command changed.
