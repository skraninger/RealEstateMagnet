"""Gemini-powered community data accumulator.

Uses Gemini API with web search grounding to accumulate comprehensive
community data - similar to using gemini.google.com in a browser but automated.

Usage:
    python -m modules.community.gemini_condenser --mode discover --limit 50
    python -m modules.community.gemini_condenser --mode enrich --community "Pelican Bay"
    python -m modules.community.gemini_condenser --mode accumulate --limit 100
"""

import argparse
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .database import CommunityDatabase
from .models import (
    CondensedCommunityBatch,
    CondensedCommunityItem,
)
from .store import CommunityStore, slugify

logger = logging.getLogger(__name__)

GEMINI_DISCOVER_PROMPT = """\
You are a real-estate expert researching gated communities in southern Florida.

Your task is to provide a comprehensive list of gated communities in the specified area.

For each community, provide:
- Full name
- City and county
- Whether it's gated (yes/no)
- Brief overview (1-2 sentences)
- HOA fees if known (monthly/annual amount)
- Key amenities
- Your confidence in the accuracy (0.0-1.0)

Focus on well-known communities. Include variety:
- Luxury/ultra-luxury communities
- 55+ active adult communities
- Master-planned communities
- Golf communities
- Waterfront communities

Use your web search grounding to find current, accurate information.
"""

GEMINI_ENRICH_PROMPT = """\
You are a real-estate expert providing detailed information about a specific gated community.

Research this community thoroughly using web search grounding:

Community: {community_name}
{location_hint}

Provide comprehensive information about:

1. **Fees** — HOA dues (monthly/annual), CDD assessments, other recurring charges
2. **Amenities** — pools, clubhouse, golf, tennis, pickleball, trails, fitness, security, pet policies
3. **Demographics** — median age, income, owner-occupancy %, population (if available)
4. **Proximity** — nearest shopping, grocery, library, hospital, school (with approximate distances)
5. **Overview** — brief description of the community

Be thorough and cite your sources when possible. Only report facts you've verified via web search.
"""


@dataclass
class GeminiCondenserResult:
    community: str
    slug: str
    fees: int = 0
    amenities: int = 0
    proximity: int = 0
    demographics_present: bool = False
    overview: str = ""
    confidence: float = 0.0
    sources_consulted: int = 0
    elapsed_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "community": self.community,
            "slug": self.slug,
            "fees": self.fees,
            "amenities": self.amenities,
            "demographics_present": self.demographics_present,
            "proximity": self.proximity,
            "overview": self.overview,
            "confidence": self.confidence,
            "sources_consulted": self.sources_consulted,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "errors": self.errors,
        }


class GeminiCondenser:
    """Uses Gemini API with web search grounding to accumulate community data.

    This is equivalent to using gemini.google.com in a browser but automated.
    Requires a Gemini API key (free tier: https://aistudio.google.com/apikey)
    """

    def __init__(
        self,
        model: Any | None = None,
        store: CommunityStore | None = None,
        database: CommunityDatabase | None = None,
        verbose: bool = False,
        show_thinking: bool = False,
        on_progress: Optional[Callable[[str], None]] = None,
        on_discovery: Optional[Callable[[CondensedCommunityItem], None]] = None,
        on_enrichment: Optional[Callable[[CondensedCommunityItem], None]] = None,
    ) -> None:
        self.store = store or CommunityStore()
        self.database = database or CommunityDatabase()
        self.verbose = verbose
        self.show_thinking = show_thinking
        self.on_progress = on_progress
        self.on_discovery = on_discovery
        self.on_enrichment = on_enrichment

        # Initialize Gemini model
        if model is not None:
            self.model = model
        else:
            api_key = os.environ.get("MODEL_API_KEY")
            base_url = os.environ.get("MODEL_BASE_URL", "")
            model_name = os.environ.get("MODEL_NAME", "gemini-2.5-flash")

            if "generativelanguage.googleapis.com" not in base_url:
                raise ValueError(
                    "Gemini not configured. Update .env with:\n"
                    "  MODEL_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai\n"
                    "  MODEL_NAME=gemini-2.5-flash\n"
                    "  MODEL_API_KEY=your-key-here\n"
                    "Get a free key at: https://aistudio.google.com/apikey"
                )

            self.model = OpenAIChatModel(
                model_name,
                provider=OpenAIProvider(
                    base_url=base_url,
                    api_key=api_key,
                ),
            )

    def _report_progress(self, message: str) -> None:
        if self.on_progress:
            self.on_progress(message)
        logger.info(message)

    def _report_discovery(self, item: CondensedCommunityItem) -> None:
        if self.on_discovery:
            self.on_discovery(item)
        logger.info("Discovered: %s (%s)", item.name, item.city or "unknown location")

    def _report_enrichment(self, item: CondensedCommunityItem) -> None:
        if self.on_enrichment:
            self.on_enrichment(item)
        logger.info(
            "Enriched: %s - %d fees, %d amenities, demographics=%s",
            item.name,
            len(item.fees),
            len(item.amenities),
            "yes" if item.demographics else "no",
        )

    def _save_item(self, item: CondensedCommunityItem) -> str:
        record = item.to_record()
        self.store.save_community(record)
        self.database.upsert_record(record, data_source="gemini_condenser")
        return record.identity.slug

    async def discover_communities(
        self,
        query: str = "gated communities in southern Florida (Miami-Dade, Broward, Palm Beach counties)",
        limit: int = 20,
    ) -> list[CondensedCommunityItem]:
        """Discover communities using Gemini with web search grounding."""
        self._report_progress(f"Gemini query: {query}")
        self._report_progress(f"Requesting up to {limit} communities")

        agent = Agent(
            self.model,
            system_prompt=GEMINI_DISCOVER_PROMPT,
            output_type=CondensedCommunityBatch,
        )

        prompt = f"List up to {limit} gated communities matching: {query}"

        if self.verbose:
            self._report_progress("\n" + "=" * 80)
            self._report_progress("GEMINI PROMPT:")
            self._report_progress("=" * 80)
            self._report_progress(prompt)
            self._report_progress("=" * 80 + "\n")

        started = time.monotonic()
        result = await agent.run(prompt)
        elapsed = time.monotonic() - started
        batch = result.output

        if self.verbose:
            self._report_progress(f"Gemini discovered {len(batch.communities)} communities in {elapsed:.2f}s")

        if self.show_thinking:
            self._report_progress("\n" + "-" * 80)
            self._report_progress("COMMUNITIES DISCOVERED BY GEMINI:")
            self._report_progress("-" * 80)
            for i, comm in enumerate(batch.communities, 1):
                self._report_progress(f"{i}. {comm.name} ({comm.city or 'unknown'})")
                if comm.overview:
                    self._report_progress(f"   {comm.overview[:150]}...")
                if comm.fees:
                    fees_str = ", ".join(f"{f.fee_type}: ${f.amount or '?'}" for f in comm.fees[:3])
                    self._report_progress(f"   Fees: {fees_str}")
                if comm.amenities:
                    amenities = ", ".join(a.amenity for a in comm.amenities[:5])
                    self._report_progress(f"   Amenities: {amenities}")
            self._report_progress("-" * 80 + "\n")

        for comm in batch.communities:
            self._report_discovery(comm)
            self._save_item(comm)

        return batch.communities

    async def enrich_community(self, name: str, city: Optional[str] = None) -> CondensedCommunityItem:
        """Enrich a community using Gemini with web search grounding."""
        location_hint = f"Location: {city}, Florida" if city else ""
        self._report_progress(f"Gemini researching: {name}")

        agent = Agent(
            self.model,
            system_prompt=GEMINI_ENRICH_PROMPT.format(
                community_name=name,
                location_hint=location_hint,
            ),
            output_type=CondensedCommunityItem,
        )

        prompt = f"Research the gated community '{name}'{f' in {city}' if city else ''} thoroughly."

        if self.verbose:
            self._report_progress("\n" + "=" * 80)
            self._report_progress("GEMINI PROMPT:")
            self._report_progress("=" * 80)
            self._report_progress(prompt)
            self._report_progress("=" * 80 + "\n")

        started = time.monotonic()
        result = await agent.run(prompt)
        elapsed = time.monotonic() - started
        item = result.output

        if self.verbose:
            self._report_progress(f"Gemini enriched {name} in {elapsed:.2f}s")
            self._report_progress(f"Extracted: {len(item.fees)} fees, {len(item.amenities)} amenities")

        if self.show_thinking:
            self._report_progress("\n" + "-" * 80)
            self._report_progress("EXTRACTED DETAILS FOR {name}:")
            self._report_progress("-" * 80)
            if item.fees:
                self._report_progress("Fees:")
                for fee in item.fees:
                    self._report_progress(f"  - {fee.fee_type}: ${fee.amount or 'unknown'}")
            if item.amenities:
                self._report_progress("Amenities:")
                for amenity in item.amenities:
                    self._report_progress(f"  - {amenity.amenity}")
            if item.demographics:
                self._report_progress(f"Demographics: population={item.demographics.population}")
            if item.proximity:
                self._report_progress("Proximity:")
                for prox in item.proximity:
                    self._report_progress(f"  - {prox.category}: {prox.nearest_name}")
            self._report_progress("-" * 80 + "\n")

        self._report_enrichment(item)
        self._save_item(item)
        return item

    async def run_discover(self, limit: int = 50) -> list[GeminiCondenserResult]:
        """Discover communities from Gemini."""
        queries = [
            "gated communities in Miami-Dade County Florida",
            "gated communities in Broward County Florida",
            "gated communities in Palm Beach County Florida",
            "luxury gated communities south florida",
            "55+ active adult communities florida",
        ]

        all_items: list[CondensedCommunityItem] = []
        results: list[GeminiCondenserResult] = []

        for query in queries:
            if len(all_items) >= limit:
                break
            items = await self.discover_communities(query, limit=min(20, limit - len(all_items)))
            all_items.extend(items)

        # Deduplicate
        seen = set()
        for item in all_items:
            if item.name.lower() not in seen:
                seen.add(item.name.lower())
                results.append(GeminiCondenserResult(
                    community=item.name,
                    slug=slugify(item.name),
                    fees=len(item.fees),
                    amenities=len(item.amenities),
                    proximity=len(item.proximity),
                    demographics_present=item.demographics is not None,
                    overview=item.overview or "",
                    confidence=item.confidence,
                ))

        return results

    async def run_enrich(
        self,
        communities: Optional[list[str]] = None,
        limit: int = 20,
    ) -> list[GeminiCondenserResult]:
        """Enrich communities using Gemini."""
        if communities is None:
            targets = self.store.load_target_list()
            communities = [t["name"] for t in targets[:limit]]

        results: list[GeminiCondenserResult] = []
        for name in communities:
            started = time.monotonic()
            report = GeminiCondenserResult(community=name, slug=slugify(name))
            try:
                seed = next(
                    (t for t in self.store.load_target_list() if t["name"].lower() == name.lower()),
                    None,
                )
                city = seed.get("city") if seed else None
                item = await self.enrich_community(name, city=city)
                report.slug = slugify(item.name)
                report.fees = len(item.fees)
                report.amenities = len(item.amenities)
                report.proximity = len(item.proximity)
                report.demographics_present = item.demographics is not None
                report.overview = item.overview or ""
                report.confidence = item.confidence
            except Exception as exc:
                report.errors.append(str(exc))
            report.elapsed_seconds = time.monotonic() - started
            results.append(report)

        return results

    async def run_accumulate(self, limit: int = 100) -> list[GeminiCondenserResult]:
        """Discover + enrich in a loop for comprehensive data."""
        self._report_progress("Starting accumulation loop with Gemini")

        # Phase 1: Discover
        discover_results = await self.run_discover(limit=limit)
        self._report_progress(f"Discovered {len(discover_results)} communities")

        # Phase 2: Enrich top discovered communities
        discovered_names = [r.community for r in discover_results[:min(20, len(discover_results))]]
        if discovered_names:
            self._report_progress(f"Enriching top {len(discovered_names)} discovered communities")
            enrich_results = await self.run_enrich(communities=discovered_names, limit=len(discovered_names))
            return discover_results + enrich_results

        return discover_results

    def get_summary(self) -> dict[str, Any]:
        return self.database.summary()


async def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Gemini-powered community data accumulator with web search grounding."
    )
    parser.add_argument(
        "--mode",
        choices=["discover", "enrich", "accumulate", "summary"],
        default="summary",
        help="Operation mode",
    )
    parser.add_argument("--limit", type=int, default=50, help="Max communities to process")
    parser.add_argument("--community", help="Enrich a specific community by name")
    parser.add_argument("--db-path", default="data/communities.db", help="SQLite database path")
    parser.add_argument("--verbose", action="store_true", help="Show prompts and responses")
    parser.add_argument("--show-thinking", action="store_true", help="Show detailed extraction")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    def on_progress(message: str) -> None:
        print(f"[PROGRESS] {message}")

    def on_discovery(item: CondensedCommunityItem) -> None:
        print(f"[DISCOVERED] {item.name} ({item.city or 'unknown'})")

    def on_enrichment(item: CondensedCommunityItem) -> None:
        print(f"[ENRICHED] {item.name}: {len(item.fees)} fees, {len(item.amenities)} amenities")

    database = CommunityDatabase(args.db_path)
    database.create_tables()

    condenser = GeminiCondenser(
        database=database,
        verbose=args.verbose,
        show_thinking=args.show_thinking,
        on_progress=on_progress,
        on_discovery=on_discovery,
        on_enrichment=on_enrichment,
    )

    if args.mode == "summary":
        summary = condenser.get_summary()
        print(json.dumps(summary, indent=2))
        return

    if args.community:
        item = await condenser.enrich_community(args.community)
        print(json.dumps({
            "community": item.name,
            "slug": slugify(item.name),
            "fees": len(item.fees),
            "amenities": len(item.amenities),
            "proximity": len(item.proximity),
            "demographics_present": item.demographics is not None,
        }, indent=2))
        return

    if args.mode == "discover":
        results = await condenser.run_discover(limit=args.limit)
    elif args.mode == "enrich":
        results = await condenser.run_enrich(limit=args.limit)
    elif args.mode == "accumulate":
        results = await condenser.run_accumulate(limit=args.limit)
    else:
        return

    for result in results:
        print(json.dumps(result.to_dict()))

    summary = condenser.get_summary()
    print(f"\n{len(results)} community/communities processed")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
