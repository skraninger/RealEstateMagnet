#!/usr/bin/env python3
"""Summarise pipeline failures from the SQLite database.

Reads:
  * ``community_condenser_runs`` — per ``(community, condenser)`` errors.
  * ``community_pipeline_status`` — per-community status + ``last_error``.

and prints a plain-text report. ``scripts/run-full-pipeline.ps1`` calls this
after every run to produce a focused, easy-to-review error log.

Usage::

    python scripts/pipeline_error_report.py --db data/communities.db
    python scripts/pipeline_error_report.py --db data/communities.db --output logs/pipeline-errors.log
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path


def build_report(db_path: str) -> str:
    """Return the full error report as a string (never raises for a missing DB)."""
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("PIPELINE ERROR REPORT")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Database:  {db_path}")
    lines.append("=" * 70)

    try:
        conn = sqlite3.connect(db_path)
    except Exception as exc:  # pragma: no cover - defensive
        lines.append(f"  Could not open database: {exc}")
        return "\n".join(lines)

    try:
        cur = conn.cursor()

        # Per-(community, condenser) errors recorded during the run.
        try:
            cur.execute(
                """
                SELECT community_slug, condenser, status, errors
                FROM community_condenser_runs
                WHERE errors IS NOT NULL AND errors != '' AND errors != '[]'
                ORDER BY community_slug, condenser
                """
            )
            rows = cur.fetchall()
            lines.append("")
            lines.append(f"Condenser errors: {len(rows)}")
            for slug, condenser, status, errors in rows:
                try:
                    messages = json.loads(errors)
                except Exception:
                    messages = [errors]
                for message in messages:
                    lines.append(f"  [{status}] {slug} ({condenser}): {message}")
        except sqlite3.OperationalError as exc:
            lines.append(f"  (community_condenser_runs unavailable: {exc})")

        # Communities left failed/partial, with the recorded reason.
        try:
            cur.execute(
                """
                SELECT community_slug, status, last_error
                FROM community_pipeline_status
                WHERE status IN ('failed', 'partial')
                ORDER BY sort_order
                """
            )
            rows = cur.fetchall()
            lines.append("")
            lines.append(f"Community status issues: {len(rows)}")
            for slug, status, last_error in rows:
                lines.append(f"  [{status}] {slug}: {last_error or ''}")
        except sqlite3.OperationalError as exc:
            lines.append(f"  (community_pipeline_status unavailable: {exc})")
    finally:
        conn.close()

    lines.append("")
    lines.append("=" * 70)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarise pipeline errors from the database."
    )
    parser.add_argument(
        "--db", default="data/communities.db", help="Path to the SQLite database."
    )
    parser.add_argument(
        "--output", default="", help="Also write the report to this file."
    )
    args = parser.parse_args()

    report = build_report(args.db)
    print(report)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report + "\n", encoding="utf-8")
        print(f"\nError report written to: {out}")


if __name__ == "__main__":
    main()
