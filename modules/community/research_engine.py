"""Community research orchestrator + CLI (Workstream B).

  python -m modules.community.research_engine --community "Pelican Bay" [--dry-run]
  python -m modules.community.research_engine --limit 3 [--dry-run]

Flow per community (plan §6):
  1. upsert identity from the target list
  2. agent researches fees / amenities / demographics / proximity
  3. facts validated by Pydantic, merged with conflict detection
  4. record saved + tool calls appended to the research log

``--dry-run`` lists the communities that would be researched; no network,
no model server needed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .agent import CommunityResearchAgent
from .store import CommunityStore, merge_facts, slugify

logger = logging.getLogger(__name__)


@dataclass
class CommunityResearchReport:
    community: str
    slug: str
    fees: int = 0
    amenities: int = 0
    proximity: int = 0
    demographics_present: bool = False
    discrepancies: list[str] = field(default_factory=list)
    tool_calls: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "community": self.community,
            "slug": self.slug,
            "dry_run": self.dry_run,
            "fees": self.fees,
            "amenities": self.amenities,
            "demographics_present": self.demographics_present,
            "proximity": self.proximity,
            "discrepancies": self.discrepancies,
            "tool_calls": self.tool_calls,
            "errors": self.errors,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
        }


class CommunityResearchEngine:
    def __init__(
        self,
        store: CommunityStore | None = None,
        agent: CommunityResearchAgent | None = None,
        url_tracker: Any | None = None,
        on_thinking: Any | None = None,
    ) -> None:
        self.store = store or CommunityStore()
        self.agent = agent or CommunityResearchAgent(
            url_tracker=url_tracker, on_thinking=on_thinking
        )

    async def research_one(self, seed: dict[str, Any]) -> CommunityResearchReport:
        report = CommunityResearchReport(
            community=seed["name"], slug=slugify(seed["name"])
        )
        started = time.monotonic()
        record = self.store.upsert_identity(seed)
        try:
            result = await self.agent.research_community(record.identity.model_dump())
        except Exception as exc:
            report.errors.append(f"research failed: {exc}")
            logger.exception("research failed for %s", seed["name"])
            report.elapsed_seconds = time.monotonic() - started
            return report

        merge_facts(record, result.facts)
        self.store.save_community(record)
        self.store.append_research_log(result.log_entries)

        report.fees = len(record.fees)
        report.amenities = len(record.amenities)
        report.demographics_present = record.demographics is not None
        report.proximity = len(record.proximity)
        report.discrepancies = [d.field for d in record.discrepancies]
        report.tool_calls = max(0, len(result.log_entries) - 1)
        report.elapsed_seconds = time.monotonic() - started
        return report

    async def run(
        self,
        community: Optional[str] = None,
        limit: Optional[int] = None,
        dry_run: bool = False,
    ) -> list[CommunityResearchReport]:
        seeds = self.store.load_target_list()
        if community is not None:
            needle = community.lower()
            seeds = [s for s in seeds if needle in s["name"].lower()]
            if not seeds:
                raise SystemExit(
                    f"no target community matching {community!r} — "
                    f"see data/communities/target_list.json"
                )
        elif limit is not None:
            seeds = seeds[:limit]

        if dry_run:
            return [
                CommunityResearchReport(
                    community=s["name"], slug=slugify(s["name"]), dry_run=True
                )
                for s in seeds
            ]

        reports: list[CommunityResearchReport] = []
        for seed in seeds:
            logger.info("researching %s", seed["name"])
            reports.append(await self.research_one(seed))
        return reports


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Research gated communities (Workstream B, local model)."
    )
    parser.add_argument("--community", help="target community name (substring match)")
    parser.add_argument("--limit", type=int, help="research at most N target-list entries")
    parser.add_argument("--dry-run", action="store_true", help="list targets, no network/model")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    reports = asyncio.run(
        CommunityResearchEngine().run(
            community=args.community, limit=args.limit, dry_run=args.dry_run
        )
    )
    for report in reports:
        print(json.dumps(report.to_dict()))
    failed = [r for r in reports if r.errors]
    print(
        f"\n{len(reports)} community/communities processed, {len(failed)} with errors"
    )


if __name__ == "__main__":
    main()
