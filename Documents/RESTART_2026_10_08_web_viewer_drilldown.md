# Restart Document — Web Viewer Community Drill-Down (documented + fix)
**Date:** 2026-10-08

> For ongoing development notes and gotchas, see the root **`AGENTS.md`**.
> This file records what changed in this session and how it was verified.

## 📍 What happened

A request was made to add a way to **descend from a community in the web viewer
(`run-web-viewer`) and show its fees, amenities, demographics, and proximity
metrics**.

Investigation showed the feature **already exists and works**:

- Route: `community_detail()` at `modules/web/viewer.py` (`GET /communities/{slug}`).
- Template: `modules/web/templates/community_detail.html`.
- Entry point: each community **name** on `/communities` links to
  `/communities/<slug>`; the Dashboard's "Recent Communities" table links there too.

No new route was required, so this session **documented** the path — and then
fixed a rendering bug found while testing every community (below).

## 🐛 Fix — 500 on communities with a median household income

`/communities/{slug}` returned **HTTP 500** for `audubon-golf-country-club`,
`azure-palm-beaches`, and `lotus-boca-raton-by-gl-homes` (any community with a
non-null `median_household_income`).

Root cause (`community_detail.html`, Demographics table):

```jinja
{{ "{:,.0f}"|format(demo.median_household_income) ... }}
```

Jinja's `format` filter is **printf-style** (`"%0.0f" % x`), so a `{:,.0f}`
placeholder produced:

```
TypeError: not all arguments converted during string formatting
```

Fix: use Python's own formatter — `'{:,.0f}'.format(demo.median_household_income)`
— which also keeps the thousands separator (`$123,456`).

Scanned **all 205 communities** through the route before/after:

- before: `total=205 failures=3`
- after: `total=205 failures=0`

Other `|format` calls in the template use valid `%`-style literals (`%.2f`,
`%.1f`, `%.0f`) and are guarded; NULL `confidence` values were checked and there
are none, so no other latent render errors exist today.

## ✅ Verification

End-to-end request against the real DB, now used via the project `.venv`:

```python
from fastapi.testclient import TestClient
from modules.web.viewer import app
r = TestClient(app).get("/communities/gables-estates")   # 200
```

- Full scan of all 205 community detail pages: **0 failures**.
- `pytest tests/test_web_viewer.py`: **3 passed**.
- `pytest tests/ -q` (`.venv`): **303 passed, 0 skipped**.

## 🧩 Environment — web extras installed into `.venv`

`.venv` had `starlette`/`uvicorn` but was missing `fastapi` and `jinja2`, so the
viewer could not import under it (and the 3 viewer tests skipped). Installed:

```powershell
.venv\Scripts\python.exe -m pip install fastapi jinja2 "uvicorn[standard]"
# -> fastapi 0.143.0, jinja2 3.1.6, MarkupSafe, httptools, watchfiles, annotated-doc
```

`jinja2` was **not** in `requirements.txt` (fastapi does not pull it), so it was
added there. A `.venv` built before this must run `pip install -r requirements.txt`
to get the viewer working.

## 📝 Docs / tests added

- `tests/test_web_viewer.py` *(new)* — 3 offline template tests (median-income
  comma formatting regression, empty-facts render, fact format filters). They
  `importorskip("jinja2")`, so they skip where the web extras aren't installed.
- `Documents/WEB_VIEWER.md`
  - New **"Drilling down into a community"** subsection (click path + URL pattern).
  - **Community Detail** rewritten into a section → source-table mapping.
  - **Database Requirements** extended from 6 to all 10 tables.
- `AGENTS.md` — repo map notes the drill-down; §6 gotcha documents the Jinja
  `format`-filter trap and the web-extras dependency; test count updated.

## 🔍 Key files

| Area | File |
|------|------|
| Viewer routes | `modules/web/viewer.py` |
| Detail template (fixed) | `modules/web/templates/community_detail.html` |
| Regression tests | `tests/test_web_viewer.py` |
| Viewer guide | `Documents/WEB_VIEWER.md` |
| Dependencies | `requirements.txt` (`jinja2` added) |
| Launcher | `scripts/run-web-viewer.ps1` → `run_viewer.py` |

---

**Status**: ✅ Drill-down documented; median-income 500 fixed and verified across all 205 communities.
