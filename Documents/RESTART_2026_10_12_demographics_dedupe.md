# Restart Document — Demographics Dedupe (cleanup + prevention)

**Date:** 2026-10-12

> For ongoing development notes and gotchas, see the root **`AGENTS.md`**.
> This file records what changed in this session and how it was verified.

## 📍 What was requested

"Many rows in demographics have duplicate data (not including geography and
source) that are not useful. Provide a process to get rid of all but the first
of the duplicates, and prevent more duplicates from posting into the file."

## 🔎 Root cause

`CommunityDatabase._update_row` merged fees, amenities and proximity with a
duplicate-key check, but appended a **`community_demographics` row
unconditionally** on every `upsert_record` against an existing slug. So each
re-run/pipeline step added another identical demographics row.

The real DB confirmed it: `community_demographics` had **3715 rows** that
collapsed to **182** once rows agreeing on the value columns were grouped.

## ✅ What changed

### Prevention (root cause)

- `modules/community/database.py`
  - New `DEMOGRAPHICS_VALUE_COLUMNS` constant — the value identity
    `(community_slug, median_age, median_household_income, owner_occupancy_pct,
    population, data_year)`.
  - `_update_row` now skips a demographics row whose value identity already
    exists for the community (mirrors the fee/amenity/proximity logic).
- `modules/community/store.py`
  - New `_demographics_key(d)` helper.
  - `merge_facts` no longer appends a `demographics_history` entry that matches
    the current demographics (or an existing history entry) on values.

### Cleanup (existing duplicates)

- `CommunityDatabase.dedupe_demographics(dry_run=False)` — deletes
  `community_demographics` rows that are not the lowest `id` within their value
  group; returns rows removed/would-remove.
- `CommunityStore.dedupe_demographics_history(dry_run=False)` — prunes
  duplicate/current-equal value entries from every stored
  `demographics_history`.
- `scripts/dedupe_demographics.py` — CLI wrapper with `--dry-run`,
  `--db-path`, `--store-root`, `--skip-db`, `--skip-files`. Idempotent.

### Applied to the real store

```
python scripts/dedupe_demographics.py
  database:   3533 duplicate row(s) removed   (3715 -> 182)
  file store: 2 duplicate history entr(y/ies) removed
```

Re-running reports 0 (idempotent); the table now has 0 duplicate groups.

## 🧪 Verification

- `.venv\Scripts\python.exe -m pytest tests\ -q` → **341 passed** (was 337).
- New offline tests (`tests/test_community.py`):
  - `TestMergeFacts::test_demographics_identical_values_do_not_duplicate_history`
  - `TestStore::test_dedupe_demographics_history`
  - `TestDatabase::test_reupsert_same_demographics_not_duplicated`
  - `TestDatabase::test_dedupe_demographics_keeps_first`
- Manual: dry-run → apply → dry-run shows 0; direct SQLite check shows 182 rows
  and 0 duplicate groups.

## ⏭️ Next / notes

- The same "append without a key" pattern exists for fees/amenities/proximity
  **only** in `_update_row` — those already dedupe. `upsert_record`'s initial
  insert path is fine (one row per new community).
- If duplicate demographics later reappear, run the script again; the
  prevention fix should stop new ones at the source.
