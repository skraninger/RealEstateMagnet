"""One-time, idempotent migration of legacy community data to the new schema.

The migration is additive: it never drops tables or columns and never deletes
data. It:

1. registers every known community (from ``pipeline_state.json``, the target
   list, ``discovery_state.json`` and the existing database) in ``communities``;
2. assigns a stable ``sort_order`` so a restart begins at the first community
   that has not been completed;
3. backfills ``community_urls`` from the legacy ``source_urls.community_slug``
   column so existing URL↔community links are retained;
4. (only when the database has no condenser runs yet) reconstructs
   ``community_condenser_runs`` and derives each community's pipeline status
   from the legacy JSON results — communities that finished all condensers are
   marked ``completed``; partially-processed ones are marked ``partial``; the
   rest stay ``pending``.

Running it repeatedly is safe.

CLI::

    python -m modules.community.migration
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from .database import (
    TERMINAL_OK_STATUSES,
    TERMINAL_STATUSES,
    CommunityDatabase,
)
from .store import CommunityStore, slugify

logger = logging.getLogger(__name__)

DEFAULT_CONDENSERS = ["ai", "web", "research", "browser", "gemini"]

DEFAULT_STATE_PATH = Path("data") / "pipeline_state.json"

# Marker recorded in schema_migrations after a successful run so subsequent
# startups skip the (heavy) legacy scan/import entirely.
MIGRATION_NAME = "pipeline_status_v1"


@dataclass
class MigrationReport:
    """Summary of what a migration run changed."""

    registered: int = 0
    sort_assigned: int = 0
    urls_backfilled: int = 0
    condenser_runs_imported: int = 0
    completed: int = 0
    partial: int = 0
    pending: int = 0
    statuses_applied: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "registered": self.registered,
            "sort_assigned": self.sort_assigned,
            "urls_backfilled": self.urls_backfilled,
            "condenser_runs_imported": self.condenser_runs_imported,
            "completed": self.completed,
            "partial": self.partial,
            "pending": self.pending,
            "statuses_applied": self.statuses_applied,
            "notes": self.notes,
        }


def _add(
    ordered: list[dict[str, Any]],
    seen: set[str],
    name: str,
    slug: Optional[str],
    city: Optional[str],
    county: Optional[str],
    source: str,
) -> None:
    if not name:
        return
    s = slug or slugify(name)
    if not s or s in seen:
        return
    seen.add(s)
    ordered.append(
        {
            "name": name,
            "slug": s,
            "city": city,
            "county": county,
            "source": source,
        }
    )


def collect_communities(
    db: CommunityDatabase,
    state_path: Optional[Path] = None,
    store_root: Path | str = Path("data") / "communities",
    discovery_state_path: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Collect every known community, deduplicated by slug and ordered.

    Order: legacy ``pipeline_state.json`` first (so the processing order matches
    the prior run), then the target list, discovery state, and the database.
    """
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1. Legacy pipeline state (preserves the previous run's ordering).
    if state_path and Path(state_path).exists():
        try:
            data = json.loads(Path(state_path).read_text(encoding="utf-8"))
            for c in data.get("communities", []):
                _add(
                    ordered,
                    seen,
                    c.get("name", ""),
                    c.get("slug"),
                    c.get("city"),
                    c.get("county"),
                    "pipeline_state",
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not read pipeline state %s: %s", state_path, exc)

    # 2. Target list.
    try:
        store = CommunityStore(store_root)
        for seed in store.load_target_list():
            _add(
                ordered,
                seen,
                seed.get("name", ""),
                None,
                seed.get("city"),
                seed.get("county_fips") or seed.get("county"),
                "target_list",
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not read target list: %s", exc)

    # 3. Discovery state.
    dsp = discovery_state_path or Path("data/communities/discovery_state.json")
    if Path(dsp).exists():
        try:
            ds = json.loads(Path(dsp).read_text(encoding="utf-8"))
            for dc in ds.get("discovered_communities", []):
                _add(
                    ordered,
                    seen,
                    dc.get("name", ""),
                    dc.get("slug"),
                    dc.get("city"),
                    dc.get("county"),
                    "discovery",
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not read discovery state %s: %s", dsp, exc)

    # 4. Existing database records.
    try:
        for record in db.list_records():
            _add(
                ordered,
                seen,
                record.identity.name,
                record.identity.slug,
                record.identity.city,
                record.identity.county_fips,
                "database",
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not list database records: %s", exc)

    return ordered


def _import_legacy_progress(
    db: CommunityDatabase,
    state_path: Optional[Path],
    condensers: list[str],
    report: MigrationReport,
) -> None:
    """Reconstruct condenser runs + statuses from legacy JSON results."""
    if not state_path or not Path(state_path).exists():
        report.notes.append("no pipeline_state.json — no legacy progress to import")
        return

    data = json.loads(Path(state_path).read_text(encoding="utf-8"))
    results: dict[str, dict[str, Any]] = data.get("results", {}) or {}
    if not results:
        report.notes.append("pipeline_state.json has no results to import")
        return

    for slug, step_map in results.items():
        if not isinstance(step_map, dict):
            continue
        # Import terminal condenser runs (done / error / skipped). "pending"
        # and stale "running" steps are left absent so they resume.
        for condenser, step in step_map.items():
            if not isinstance(step, dict):
                continue
            status = step.get("status", "pending")
            if status not in TERMINAL_STATUSES:
                continue
            db.record_condenser_run(
                slug,
                condenser,
                SimpleNamespace(
                    status=status,
                    elapsed=step.get("elapsed", 0.0),
                    fees=step.get("fees", 0),
                    amenities=step.get("amenities", 0),
                    proximity=step.get("proximity", 0),
                    demographics_present=step.get("demographics_present", False),
                    sources_consulted=step.get("sources_consulted", 0),
                    errors=step.get("errors", []),
                ),
            )
            report.condenser_runs_imported += 1

        # Derive the community's status across the full condenser set.
        statuses = {
            c: (step_map.get(c, {}) or {}).get("status", "pending")
            for c in condensers
        }
        if all(statuses.get(c) in TERMINAL_OK_STATUSES for c in condensers):
            db.mark_completed(slug)
            report.completed += 1
        elif any(s in TERMINAL_STATUSES for s in statuses.values()):
            db.mark_partial(slug)
            report.partial += 1
        else:
            report.pending += 1

    report.statuses_applied = True


def migrate_database(
    db: Optional[CommunityDatabase] = None,
    state_path: Optional[Path] = DEFAULT_STATE_PATH,
    store_root: Path | str = Path("data") / "communities",
    discovery_state_path: Optional[Path] = None,
    condensers: Optional[list[str]] = None,
    verbose: bool = False,
) -> MigrationReport:
    """Run the full, idempotent migration. Returns a :class:`MigrationReport`.

    Subsequent calls are a fast no-op once the migration marker is recorded;
    new communities discovered later are registered by the pipeline itself.
    """
    db = db or CommunityDatabase()
    db.create_tables()
    report = MigrationReport()
    condensers = condensers or list(DEFAULT_CONDENSERS)

    # Fast restart path: nothing to do if already applied.
    if db.is_migration_applied(MIGRATION_NAME):
        report.notes.append("migration already applied — skipped")
        if verbose:
            logger.info("Migration %s already applied — skipping", MIGRATION_NAME)
        return report

    # 1 + 2. Register all communities and assign processing order.
    ordered = collect_communities(db, state_path, store_root, discovery_state_path)
    report.registered = db.register_communities(ordered)
    report.sort_assigned = db.assign_sort_order([c["slug"] for c in ordered])

    # 3. Backfill the new URL↔community join table from legacy rows.
    report.urls_backfilled = db.backfill_community_urls()

    # 4. Import legacy progress only once (never clobber live progress).
    if db.count_condenser_runs() == 0:
        try:
            _import_legacy_progress(db, state_path, condensers, report)
        except Exception as exc:  # pragma: no cover - defensive
            report.notes.append(f"progress import failed: {exc}")
            logger.warning("Legacy progress import failed: %s", exc)
    else:
        report.notes.append("condenser runs already present — progress import skipped")

    db.mark_migration_applied(MIGRATION_NAME)

    if verbose:
        logger.info("Migration report: %s", report.to_dict())

    return report


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Migrate legacy community data to the pipeline-status schema."
    )
    parser.add_argument("--db-path", default="data/communities.db")
    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--store-root", default=str(Path("data") / "communities"))
    parser.add_argument(
        "--discovery-state-path",
        default="data/communities/discovery_state.json",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db = CommunityDatabase(args.db_path)
    report = migrate_database(
        db=db,
        state_path=Path(args.state_path),
        store_root=args.store_root,
        discovery_state_path=Path(args.discovery_state_path),
        verbose=True,
    )
    print(json.dumps(report.to_dict(), indent=2))


if __name__ == "__main__":
    main()
