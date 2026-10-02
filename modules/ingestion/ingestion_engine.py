"""
Ingestion orchestrator + CLI.

  python -m modules.ingestion.ingestion_engine \
      --source "Broward County Open Data" \
      [--category tax] [--protocol socrata] \
      [--limit N] [--output data/raw] [--dry-run] [--force]

Flow per source:
  1. pick fetcher by protocol (web → WebFetcher with Playwright branch)
  2. discover datasets (catalog call)
  3. for each dataset (bounded by --limit): skip if already in manifest
     unless --force, else fetch + write raw file + manifest entry
  4. return IngestReport

``--dry-run`` performs discovery but downloads nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..discovery.florida_sources import ALL_SOURCES, FloridaSource
from .fetchers import FETCHER_REGISTRY
from .polite_client import PoliteClient
from .storage import RawStore

logger = logging.getLogger(__name__)


@dataclass
class IngestReport:
    source_name: str
    protocol: str
    datasets_discovered: int = 0
    datasets_fetched: int = 0
    datasets_skipped: int = 0
    total_rows: int = 0
    total_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    planned_datasets: list[dict[str, Any]] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "protocol": self.protocol,
            "dry_run": self.dry_run,
            "datasets_discovered": self.datasets_discovered,
            "datasets_fetched": self.datasets_fetched,
            "datasets_skipped": self.datasets_skipped,
            "total_rows": self.total_rows,
            "total_bytes": self.total_bytes,
            "errors": self.errors,
            "planned_datasets": self.planned_datasets[:50],
            "elapsed_seconds": round(self.elapsed_seconds, 2),
        }


class IngestionEngine:
    """Orchestrates discovery → fetch → raw storage for one or more sources."""

    def __init__(
        self,
        client: PoliteClient | None = None,
        store_root: Path | str = Path("data") / "raw",
        limit: int | None = None,
        force: bool = False,
    ) -> None:
        self.client = client or PoliteClient()
        self.store = RawStore(store_root)
        self.limit = limit
        self.force = force

    async def ingest(
        self, source: FloridaSource, dry_run: bool = False
    ) -> IngestReport:
        report = IngestReport(source_name=source.name, protocol=source.protocol, dry_run=dry_run)
        started = time.monotonic()

        fetcher_cls = FETCHER_REGISTRY.get(source.protocol)
        if fetcher_cls is None and source.protocol == "web":
            from .fetchers.web_fetcher import WebFetcher
            fetcher_cls = WebFetcher
        if fetcher_cls is None:
            report.errors.append(f"no fetcher registered for protocol '{source.protocol}'")
            report.elapsed_seconds = time.monotonic() - started
            return report

        fetcher = fetcher_cls(self.client)

        try:
            datasets = await fetcher.discover(source)
        except Exception as exc:
            report.errors.append(f"discovery failed: {exc}")
            report.elapsed_seconds = time.monotonic() - started
            return report

        report.datasets_discovered = len(datasets)
        report.planned_datasets = [
            {"dataset_id": d.dataset_id, "name": d.name, "format": d.format_hint}
            for d in datasets[:50]
        ]

        if dry_run:
            report.elapsed_seconds = time.monotonic() - started
            return report

        for dataset in datasets[: self.limit] if self.limit else datasets:
            if not self.force and self.store.has_entry(source.name, dataset.dataset_id):
                report.datasets_skipped += 1
                continue
            try:
                result = await fetcher.fetch(source, dataset)
            except Exception as exc:
                report.errors.append(f"{dataset.dataset_id}: {exc}")
                continue
            if result.raw_bytes is None and result.records is None:
                report.errors.append(
                    f"{dataset.dataset_id}: no data ({'; '.join(result.warnings) or 'unknown'})"
                )
                continue
            try:
                self.store.write(result)
            except Exception as exc:
                report.errors.append(f"{dataset.dataset_id}: storage failed: {exc}")
                continue
            report.datasets_fetched += 1
            report.total_rows += result.row_count
            report.total_bytes += result.size_bytes
            for w in result.warnings:
                logger.warning("%s/%s: %s", source.name, dataset.dataset_id, w)

        report.elapsed_seconds = time.monotonic() - started
        return report

    async def aclose(self) -> None:
        await self.client.aclose()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def find_sources(
    name: str | None = None,
    category: str | None = None,
    protocol: str | None = None,
) -> list[FloridaSource]:
    matches = ALL_SOURCES
    if name:
        needle = name.lower()
        matches = [s for s in matches if needle in s.name.lower()]
    if category:
        matches = [s for s in matches if category in s.categories]
    if protocol:
        matches = [s for s in matches if s.protocol == protocol]
    return matches


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Phase 2 ingestion engine — harvest raw data from FL open-data sources"
    )
    parser.add_argument("--source", required=True, help="Source name (substring match)")
    parser.add_argument("--category", default=None, help="Filter by data category")
    parser.add_argument("--protocol", default=None, help="Filter by protocol")
    parser.add_argument("--limit", type=int, default=None, help="Max datasets per source")
    parser.add_argument("--output", default="data/raw", help="Raw storage root")
    parser.add_argument("--dry-run", action="store_true",
                        help="Discover and list planned datasets without downloading")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch datasets already present in the manifest")
    parser.add_argument("--delay", type=float, default=None,
                        help="Override CRAWL_DELAY_SECONDS (per-host min interval)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    sources = find_sources(args.source, args.category, args.protocol)
    if not sources:
        print(f"No sources match --source {args.source!r} "
              f"(category={args.category}, protocol={args.protocol})")
        print("Available sources:")
        for s in ALL_SOURCES[:60]:
            print(f"  [{s.protocol:8}] {s.name}")
        raise SystemExit(1)

    client = PoliteClient(min_interval=args.delay) if args.delay is not None else PoliteClient()
    engine = IngestionEngine(
        client=client, store_root=Path(args.output),
        limit=args.limit, force=args.force,
    )

    for source in sources:
        print(f"\n=== {source.name} ({source.protocol}) ===")
        report = await engine.ingest(source, dry_run=args.dry_run)
        mode = "DRY-RUN" if report.dry_run else "INGESTED"
        print(f"  {mode}: {report.datasets_fetched}/{report.datasets_discovered} datasets"
              f" (skipped {report.datasets_skipped})")
        print(f"  rows={report.total_rows:,}  bytes={report.total_bytes:,}"
              f"  elapsed={report.elapsed_seconds:.1f}s")
        for d in report.planned_datasets[:20]:
            print(f"    - {d['dataset_id'][:40]:40} {d['format']:8} {d['name'][:50]}")
        for err in report.errors[:10]:
            print(f"  ! {err}")

    await engine.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
