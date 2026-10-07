"""Tests for migrating legacy community data to the pipeline-status schema."""

from __future__ import annotations

import json
from pathlib import Path

from modules.community.database import CommunityDatabase, SourceURLRow
from modules.community.migration import migrate_database


def _write_state(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "communities": [
                    {"name": "Done Bay", "slug": "done-bay", "city": "Miami"},
                    {"name": "Partial Point", "slug": "partial-point", "city": "Naples"},
                    {"name": "Fresh Cove", "slug": "fresh-cove", "city": "Tampa"},
                ],
                "condensers": ["ai", "web", "research", "browser", "gemini"],
                "results": {
                    "done-bay": {
                        "ai": {"status": "done", "fees": 2, "amenities": 3},
                        "web": {"status": "done"},
                        "research": {"status": "done"},
                        "browser": {"status": "done"},
                        "gemini": {"status": "skipped"},
                    },
                    "partial-point": {
                        "ai": {"status": "done", "fees": 1},
                        "web": {"status": "pending"},
                        "research": {"status": "running"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def _make_db(tmp_path: Path) -> CommunityDatabase:
    db = CommunityDatabase(tmp_path / "communities.db")
    db.create_tables()
    return db


def _isolated_kwargs(tmp_path: Path) -> dict:
    """Point the migration at empty temp locations instead of real data/."""
    store_root = tmp_path / "store"
    store_root.mkdir(exist_ok=True)
    return {
        "store_root": store_root,
        "discovery_state_path": tmp_path / "discovery_state.json",
    }


def test_migration_registers_and_derives_status(tmp_path: Path) -> None:
    state_path = tmp_path / "pipeline_state.json"
    _write_state(state_path)
    db = _make_db(tmp_path)

    # Legacy URL with only the old community_slug column populated.
    with db.session() as session:
        session.add(
            SourceURLRow(
                url="https://legacy.example/page",
                domain="legacy.example",
                status="printed",
                discovered_by="research_agent",
                community_slug="done-bay",
            )
        )
        session.commit()

    report = migrate_database(db=db, state_path=state_path, **_isolated_kwargs(tmp_path))

    # All communities registered (three from the legacy state).
    assert report.registered == 3
    assert db.count() == 3
    assert report.sort_assigned == 3

    # URL link backfilled into the new join table.
    assert report.urls_backfilled == 1
    assert len(db.get_urls_for_community("done-bay")) == 1

    # Statuses derived from legacy progress.
    assert db.get_pipeline_status("done-bay") == "completed"
    assert db.get_pipeline_status("partial-point") == "partial"
    assert db.get_pipeline_status("fresh-cove") == "pending"
    assert report.completed == 1
    assert report.partial == 1

    # Terminal condenser runs imported; stale "running" is left to resume.
    runs = db.get_condenser_runs("partial-point")
    assert runs["ai"]["status"] == "done"
    assert runs["ai"]["fees"] == 1
    assert "research" not in runs  # running -> absent -> resumes
    assert "web" not in runs  # pending -> absent


def test_migration_is_idempotent(tmp_path: Path) -> None:
    state_path = tmp_path / "pipeline_state.json"
    _write_state(state_path)
    db = _make_db(tmp_path)

    kwargs = _isolated_kwargs(tmp_path)
    first = migrate_database(db=db, state_path=state_path, **kwargs)
    assert first.registered == 3
    runs_after_first = db.count_condenser_runs()

    assert db.is_migration_applied("pipeline_status_v1")

    second = migrate_database(db=db, state_path=state_path, **kwargs)

    # The second run is a fast no-op once the marker is recorded.
    assert any("already applied" in note for note in second.notes)
    assert second.registered == 0
    # No duplicate communities are created.
    assert db.count() == 3
    # Progress import is skipped on the second run (already present).
    assert second.condenser_runs_imported == 0
    assert db.count_condenser_runs() == runs_after_first
    # Existing status is preserved (not clobbered back to pending).
    assert db.get_pipeline_status("done-bay") == "completed"


def test_migration_sort_order_is_stable(tmp_path: Path) -> None:
    state_path = tmp_path / "pipeline_state.json"
    _write_state(state_path)
    db = _make_db(tmp_path)

    migrate_database(db=db, state_path=state_path, **_isolated_kwargs(tmp_path))
    statuses = db.list_pipeline_statuses()

    assert [s["slug"] for s in statuses] == ["done-bay", "partial-point", "fresh-cove"]
    assert [s["sort_order"] for s in statuses] == [0, 1, 2]
    # Restart begins at the first non-completed community.
    assert db.next_incomplete_community() == "partial-point"
