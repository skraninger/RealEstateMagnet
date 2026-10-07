"""Database migration entry point.

Two stages, both idempotent and additive (no data is ever dropped):

1. Legacy column patch — add any missing ``source_urls`` columns so very old
   databases keep working.
2. New pipeline-status schema migration — register every known community, assign
   a stable processing order, backfill the ``community_urls`` join table, and
   reconstruct per-condenser progress from ``data/pipeline_state.json``. See
   ``modules/community/migration.py`` for details.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB_PATH = Path("data") / "communities.db"


def add_missing_source_url_columns(db_path: Path = DEFAULT_DB_PATH) -> int:
    """Add missing columns to the legacy source_urls table. Returns count added."""
    if not db_path.exists():
        print("Database not found, skipping legacy column patch")
        return 0

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='source_urls'"
    )
    if not cursor.fetchone():
        print("source_urls table does not exist, skipping legacy column patch")
        conn.close()
        return 0

    cursor.execute("PRAGMA table_info(source_urls)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    new_columns = [
        ("robot_friendly", "BOOLEAN DEFAULT 0"),
        ("robots_txt_checked", "BOOLEAN DEFAULT 0"),
        ("has_community_data", "BOOLEAN DEFAULT 0"),
        ("data_quality_score", "REAL DEFAULT 0.0"),
        ("data_types_found", "TEXT"),
        ("data_summary", "TEXT"),
    ]

    added_count = 0
    for col_name, col_type in new_columns:
        if col_name not in existing_columns:
            print(f"Adding column: {col_name} ({col_type})")
            cursor.execute(f"ALTER TABLE source_urls ADD COLUMN {col_name} {col_type}")
            added_count += 1
        else:
            print(f"Column already exists: {col_name}")

    conn.commit()
    conn.close()
    return added_count


def migrate_database(db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Run the full migration and return the new-schema migration report."""
    from modules.community.database import CommunityDatabase
    from modules.community.migration import migrate_database as run_schema_migration

    added = add_missing_source_url_columns(db_path)
    print(f"\nLegacy column patch: added {added} column(s)")

    print("\nRunning pipeline-status schema migration...")
    db = CommunityDatabase(db_path)
    report = run_schema_migration(db=db, verbose=True)
    print("\nMigration complete.")
    print(json.dumps(report.to_dict(), indent=2))
    return report.to_dict()


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB_PATH
    migrate_database(path)
