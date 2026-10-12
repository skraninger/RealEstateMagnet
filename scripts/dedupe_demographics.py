#!/usr/bin/env python3
"""Remove duplicate demographics data (keep the first) and report what changed.

Two demographics entries are treated as duplicates when they share a community
and every *value* column — ``median_age``, ``median_household_income``,
``owner_occupancy_pct``, ``population`` and ``data_year``. ``geography_level``,
``source_url``, ``confidence`` and ``retrieved_at`` are deliberately ignored
(per the request), so re-running research no longer stacks identical rows.

Cleans both stores:
  * ``community_demographics`` rows in ``data/communities.db`` (keeps lowest id).
  * ``demographics_history`` arrays in ``data/communities/communities/*.json``.

Usage::

    # See what would be removed without changing anything
    python scripts/dedupe_demographics.py --dry-run

    # Apply
    python scripts/dedupe_demographics.py

    # Custom locations
    python scripts/dedupe_demographics.py --db-path data/communities.db \
        --store-root data/communities
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow ``python scripts/dedupe_demographics.py`` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.community.database import CommunityDatabase  # noqa: E402
from modules.community.store import CommunityStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Remove duplicate demographics rows/entries, keeping the first."
    )
    parser.add_argument(
        "--db-path",
        default="data/communities.db",
        help="SQLite database (default: data/communities.db)",
    )
    parser.add_argument(
        "--store-root",
        default="data/communities",
        help="File store root (default: data/communities)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be removed without writing anything",
    )
    parser.add_argument(
        "--skip-db", action="store_true", help="Do not touch the SQLite table"
    )
    parser.add_argument(
        "--skip-files", action="store_true", help="Do not touch the JSON store"
    )
    args = parser.parse_args(argv)

    mode = "DRY RUN — no changes written" if args.dry_run else "APPLYING changes"
    print(f"Deduplicating demographics ({mode})")
    print(f"  database:   {args.db_path}")
    print(f"  file store: {args.store_root}")

    db_removed = 0
    if not args.skip_db:
        db = CommunityDatabase(args.db_path)
        db_removed = db.dedupe_demographics(dry_run=args.dry_run)
        print(
            f"  database:   {db_removed} duplicate row(s) "
            f"{'would be' if args.dry_run else ''} removed"
        )

    file_removed = 0
    if not args.skip_files:
        store = CommunityStore(args.store_root)
        file_removed = store.dedupe_demographics_history(dry_run=args.dry_run)
        print(
            f"  file store: {file_removed} duplicate history entr(y/ies) "
            f"{'would be' if args.dry_run else ''} removed"
        )

    total = db_removed + file_removed
    verb = "would be removed" if args.dry_run else "removed"
    print(f"Done — {total} duplicate(s) {verb}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
