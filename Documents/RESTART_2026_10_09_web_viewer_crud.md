# Restart Document — Web Viewer Inline Edit/Delete (CRUD)

**Date:** 2026-10-09

> For ongoing development notes and gotchas, see the root **`AGENTS.md`**.
> This file records what changed in this session and how it was verified.

## 📍 What was requested

"Add functions to the web viewer to delete and modify rows."

Clarified with the user before building:

- **Scope:** community facts (fees/amenities/demographics/proximity),
  pipeline status + condenser runs, and source URLs.
  _(Community identity rows and the `/communities` list were left read-only.)_
- **Edit style:** inline edit + delete (no separate form pages, no bulk actions).
- **Auth:** none (local viewer, binds to `127.0.0.1`).

## ✅ What changed

### Backend — `modules/web/viewer.py`

- New config-driven, **whitelisted** field lists:
  `FACT_TABLES` (fees/amenities/demographics/proximity), `PIPELINE_FIELDS`,
  `CONDENSER_RUN_FIELDS`, `URL_FIELDS`, plus status enums.
- Helpers `_coerce_value()` (HTML form → Python type, incl. bool checkboxes) and
  `_apply_fields()` (builds an `UPDATE` from trusted column literals; values bound
  as parameters).
- Write endpoints (all return **303 See Other** back to the page):
  - `POST /communities/{slug}/facts/{table}/{id}/edit|delete`
  - `POST /communities/{slug}/pipeline/edit|delete`
  - `POST /communities/{slug}/condenser-runs/{id}/edit|delete`
  - `POST /urls/{id}/edit|delete`
- `/communities/{slug}` and `/urls` accept `?edit_table=&edit_id=` and pass edit
  context to the templates; `/urls` now selects `su.id` and the review/failure
  columns it can edit.
- `source_urls.url` is **not** editable (UNIQUE key). Deleting a URL also deletes
  its `community_urls` links (SQLite FKs aren't enforced).

### Templates

- New shared macro `modules/web/templates/_macros.html::actions` renders
  `Edit`/`Delete` (display) or `Save`/`Cancel` (edit). Row inputs connect to an
  empty `<form id="{table}-edit-{row_id}">` via the HTML5 `form="..."` attribute
  so no `<form>` sits inside `<tr>`.
- `community_detail.html` — Actions column + edit variants for fees, amenities,
  demographics, proximity, pipeline status, and condenser runs. Added Note,
  Geography, and Confidence columns where the edit form exposes those fields.
- `urls.html` — Actions column; edit mode shows a `colspan` grid with all
  editable URL fields.
- `base.html` — button/input styles for the edit UI.

### Dependencies / docs

- `requirements.txt`: added `python-multipart>=0.0.9`.
- `AGENTS.md`: repo map, test count (313), and §6 gotchas for the CRUD design.
- `Documents/WEB_VIEWER.md`: new "Editing and deleting rows" section; deps and
  future-enhancements updated.

## 🧪 Verification

- New tests (`tests/test_web_viewer.py::TestViewerWriteEndpoints`) run against a
  **temp SQLite DB** with `fastapi.testclient`:
  - edit + delete a fee; blank required field leaves the column unchanged
  - edit + delete `community_pipeline_status`; invalid enum ignored, other fields applied
  - edit a condenser run (valid `errors` JSON applied, invalid JSON ignored)
  - delete a condenser run
  - edit + delete a `source_urls` row (and its `community_urls` link)
  - unknown table / missing row → 404
  - edit-mode rendering: `form=` ids and Save/Cancel are present
- `pytest tests/test_web_viewer.py -q` → **14 passed**
- `pytest tests/ -q` → **314 passed** (was 303)

## ⚠️ Notes for next session

- No ROW INSERTION yet — only modify/delete existing rows. Adding facts still goes
  through the pipeline/condensers. If desired, add an "Add row" form per table.
- `/review-queue` and `/high-quality` remain read-only views; editing lives on
  `/urls`. (A "mark reviewed" shortcut on the queue would be a small follow-up.)
- The viewer has **no authentication**; keep it bound to `127.0.0.1`.

## 🔍 Key files

| Area | File |
|------|------|
| Viewer routes + field config | `modules/web/viewer.py` |
| Shared action macro | `modules/web/templates/_macros.html` |
| Detail page (facts/pipeline/runs) | `modules/web/templates/community_detail.html` |
| URL list page | `modules/web/templates/urls.html` |
| Styles | `modules/web/templates/base.html` |
| Tests | `tests/test_web_viewer.py` |
| Deps | `requirements.txt` |

---

**Status**: ✅ Inline edit/delete for facts, pipeline status, condenser runs, and source URLs; 313 tests passing.
