"""AI Condenser — leverage AI knowledge to quickly populate the community database.

Unlike the research agent (which searches + reads pages per community), the condenser
asks the AI directly for structured data about communities it already knows about.
This is orders of magnitude faster for initial data gathering.

The condenser works in two modes:
- **discover**: Ask the AI to list communities with basic info (name, city, county)
- **enrich**: For known communities, ask the AI for detailed facts (fees, amenities, etc.)

Supports any OpenAI-compatible API:
- Local model via llama.cpp (set MODEL_BASE_URL=http://localhost:8080/v1)
- Google Gemini (set MODEL_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/v1)
- OpenAI (set MODEL_BASE_URL=https://api.openai.com/v1)

CLI::

    python -m modules.community.ai_condenser --mode discover --limit 100
    python -m modules.community.ai_condenser --mode enrich --limit 50
    python -m modules.community.ai_condenser --mode full --limit 200
    python -m modules.community.ai_condenser --mode summary
    python -m modules.community.ai_condenser --mode discover --limit 5 --verbose
    python -m modules.community.ai_condenser --mode discover --limit 5 --show-thinking

Data is stored in both:
- ``data/communities/communities/<slug>.json`` (file-based store)
- ``data/communities.db`` (SQLite database)

Progress Monitoring:
    Use ``--verbose`` to see prompts, responses, and parsed output.
    Use ``--show-thinking`` to see the AI's reasoning process (if supported by model).
    Use ``--progress-callback`` for programmatic progress updates.
"""

from __future__ import annotations

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

from .streaming import run_agent_streamed
from .database import CommunityDatabase
from .models import (
    AI_SOURCE_URL,
    AmenityFact,
    CondensedCommunityBatch,
    CondensedCommunityItem,
    CommunityFacts,
    CommunityRecord,
    Demographics,
    FeeFact,
    ProximityMetric,
    utcnow,
)
from .store import CommunityStore, merge_facts, slugify

logger = logging.getLogger(__name__)

DISCOVERY_SYSTEM_PROMPT = """\
You are a real-estate expert specializing gated communities in southern Florida.

Your task is to list gated communities in southern Florida (Miami-Dade, Broward, \
Palm Beach, Collier, and Martin counties) based on your knowledge.

For each community, provide:
- Full community name
- City/location
- County
- Whether it is gated (yes/no)
- A brief overview (1-2 sentences)
- HOA name if known
- Your confidence level (0.0 to 1.0) about accuracy

Focus on well-known communities. Include a mix of:
- Luxury/ultra-luxury communities
- 55+ active adult communities
- Master-planned communities
- Golf communities
- Waterfront communities

Do NOT include:
- Communities outside southern Florida
- Non-residential properties
- Individual home listings
- Apartment complexes (unless gated with HOA)

Be thorough and accurate. If you are unsure about a detail, use a lower confidence score.
"""

ENRICHMENT_SYSTEM_PROMPT = """\
You are a real-estate expert specializing gated communities in southern Florida.

You will be asked about a specific community. Provide all information you know about:

1. **Fees** — HOA dues (monthly or annual amounts), CDD special assessments, \
   any other recurring charges. Use the fee_type values: hoa_monthly, hoa_annual, \
   cdd_assessment, or other.

2. **Amenities** — pool, clubhouse, golf, tennis, pickleball, trails, fitness center, \
   gate/security details, pet policies, marina, etc.

3. **Demographics** — median age, median household income, owner-occupancy %, \
   population (if you know approximate figures from Census/ACS data).

4. **Proximity** — nearest shopping/grocery, library, hospital, school, with \
   approximate distance in miles.

Rules:
- Only report facts you are reasonably confident about.
- Use the ai:condensed source URL for all facts (this is AI-knowledge data, not from a specific web page).
- Set confidence based on your certainty: 0.8+ for well-known facts, 0.5-0.8 for approximate, below 0.5 for uncertain.
- If you do not know something, leave it null/empty rather than guessing.
- For fees, report exact amounts if known, otherwise provide a range in the note field.
"""


@dataclass
class CondenserResult:
    community: str
    slug: str
    fees: int = 0
    amenities: int = 0
    proximity: int = 0
    demographics_present: bool = False
    overview: str = ""
    confidence: float = 0.0
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
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "errors": self.errors,
        }


class AICondenser:
    """Uses an AI model to quickly condense community knowledge into structured data.

    Configure via environment variables:
    - MODEL_BASE_URL: OpenAI-compatible API endpoint
    - MODEL_NAME: Model identifier
    - MODEL_API_KEY: API key (use 'local' for llama.cpp, real key for cloud APIs)
    - MODEL_MAX_TOKENS: Max output tokens per response
    - CONDENSER_BATCH_SIZE: Communities per batch query (default: 20)

    Progress monitoring:
    - verbose: If True, logs prompts, responses, and parsed output
    - show_thinking: If True, logs the AI's reasoning process (if supported)
    - on_progress: Callback function for progress updates
    - on_discovery: Callback when a community is discovered
    - on_enrichment: Callback when a community is enriched

    For local observability, use ``modules.community.observability`` or the
    pipeline's ``--trace-file`` option.
    """

    def __init__(
        self,
        model: Any | None = None,
        model_name: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        max_tokens: int | None = None,
        batch_size: int | None = None,
        store: CommunityStore | None = None,
        database: CommunityDatabase | None = None,
        verbose: bool = False,
        show_thinking: bool = False,
        on_progress: Optional[Callable[[str], None]] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_discovery: Optional[Callable[[CondensedCommunityItem], None]] = None,
        on_enrichment: Optional[Callable[[CondensedCommunityItem], None]] = None,
    ) -> None:
        self.model = model
        self.model_name = model_name or os.environ.get("MODEL_NAME", "local")
        self.base_url = base_url or os.environ.get(
            "MODEL_BASE_URL", "http://localhost:8080/v1"
        )
        self.api_key = api_key or os.environ.get("MODEL_API_KEY", "local")
        self.max_tokens = max_tokens or int(os.environ.get("MODEL_MAX_TOKENS", "8192"))
        self.batch_size = batch_size or int(os.environ.get("CONDENSER_BATCH_SIZE", "20"))
        self.store = store or CommunityStore()
        self.database = database or CommunityDatabase()
        self.verbose = verbose
        self.show_thinking = show_thinking
        self.on_progress = on_progress
        self.on_thinking = on_thinking
        self.on_discovery = on_discovery
        self.on_enrichment = on_enrichment

    def _build_model(self) -> Any:
        if self.model is not None:
            return self.model
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        return OpenAIChatModel(
            self.model_name,
            provider=OpenAIProvider(
                base_url=self.base_url,
                api_key=self.api_key,
            ),
        )

    def _report_progress(self, message: str) -> None:
        """Report progress via callback and logger."""
        if self.on_progress:
            self.on_progress(message)
        logger.info(message)

    def _report_discovery(self, item: CondensedCommunityItem) -> None:
        """Report a discovered community."""
        if self.on_discovery:
            self.on_discovery(item)
        logger.info("Discovered: %s (%s)", item.name, item.city or "unknown location")

    def _report_enrichment(self, item: CondensedCommunityItem) -> None:
        """Report an enriched community."""
        if self.on_enrichment:
            self.on_enrichment(item)
        logger.info(
            "Enriched: %s - %d fees, %d amenities, demographics=%s",
            item.name,
            len(item.fees),
            len(item.amenities),
            "yes" if item.demographics else "no",
        )

    async def discover_communities(
        self,
        query: str = "gated communities in southern Florida (Miami-Dade, Broward, Palm Beach counties)",
        limit: int = 100,
    ) -> list[CondensedCommunityItem]:
        self._report_progress(f"Querying AI for: {query}")
        self._report_progress(f"Requesting up to {limit} communities from {self.model_name}")

        agent = Agent(
            self._build_model(),
            output_type=CondensedCommunityBatch,
            system_prompt=DISCOVERY_SYSTEM_PROMPT,
            retries=2,
        )

        prompt = (
            f"List up to {limit} gated communities matching: {query}\n\n"
            f"Include diverse communities across the target counties. "
            f"For each, provide name, city, county, whether gated, overview, and confidence."
        )

        if self.verbose:
            self._report_progress("\n" + "=" * 80)
            self._report_progress("PROMPT SENT TO AI:")
            self._report_progress("=" * 80)
            self._report_progress(prompt)
            self._report_progress("=" * 80 + "\n")

        try:
            started = time.monotonic()
            batch: CondensedCommunityBatch = await run_agent_streamed(
                agent,
                prompt,
                model_settings={"max_tokens": self.max_tokens},
                on_thinking=self.on_thinking,
            )
            elapsed = time.monotonic() - started

            if self.verbose:
                self._report_progress("\n" + "=" * 80)
                self._report_progress("RAW AI RESPONSE:")
                self._report_progress("=" * 80)
                self._report_progress(f"Discovered {len(batch.communities)} communities in {elapsed:.2f}s")
                if batch.notes:
                    self._report_progress(f"AI notes: {batch.notes}")
                self._report_progress("=" * 80 + "\n")

            if self.show_thinking:
                self._report_progress("\n" + "-" * 80)
                self._report_progress("COMMUNITIES DISCOVERED:")
                self._report_progress("-" * 80)
                for i, comm in enumerate(batch.communities, 1):
                    self._report_progress(f"{i}. {comm.name}")
                    self._report_progress(f"   City: {comm.city or 'unknown'}")
                    self._report_progress(f"   County: {comm.county or 'unknown'}")
                    self._report_progress(f"   Gated: {comm.is_gated}")
                    self._report_progress(f"   Confidence: {comm.confidence:.2f}")
                    if comm.overview:
                        self._report_progress(f"   Overview: {comm.overview[:200]}...")
                    self._report_progress("")
                self._report_progress("-" * 80 + "\n")

            self._report_progress(
                f"Successfully discovered {len(batch.communities)} communities "
                f"from {self.model_name} in {elapsed:.2f}s"
            )

            for comm in batch.communities:
                self._report_discovery(comm)

            return batch.communities
        except Exception as exc:
            logger.error("Discovery failed: %s", exc)
            self._report_progress(f"ERROR: Discovery failed: {exc}")
            raise

    async def enrich_community(self, name: str, city: Optional[str] = None) -> CondensedCommunityItem:
        location = f" in {city}, Florida" if city else ""
        self._report_progress(f"Enriching community: {name}{location}")
        self._report_progress(f"Using model: {self.model_name}")

        agent = Agent(
            self._build_model(),
            output_type=CondensedCommunityItem,
            system_prompt=ENRICHMENT_SYSTEM_PROMPT,
            retries=2,
        )

        prompt = (
            f"Provide detailed information about the gated community '{name}'{location}.\n\n"
            f"Include fees (HOA dues, CDD), amenities, demographics, and proximity to "
            f"shopping, libraries, hospitals, and schools.\n"
            f"Use 'ai:condensed' as the source_url for all facts."
        )

        if self.verbose:
            self._report_progress("\n" + "=" * 80)
            self._report_progress("PROMPT SENT TO AI:")
            self._report_progress("=" * 80)
            self._report_progress(prompt)
            self._report_progress("=" * 80 + "\n")

        try:
            started = time.monotonic()
            item: CondensedCommunityItem = await run_agent_streamed(
                agent,
                prompt,
                model_settings={"max_tokens": self.max_tokens},
                on_thinking=self.on_thinking,
            )
            elapsed = time.monotonic() - started

            if self.verbose:
                self._report_progress("\n" + "=" * 80)
                self._report_progress("RAW AI RESPONSE:")
                self._report_progress("=" * 80)
                self._report_progress(f"Enriched {name} in {elapsed:.2f}s")
                self._report_progress(f"Fees: {len(item.fees)}")
                self._report_progress(f"Amenities: {len(item.amenities)}")
                self._report_progress(f"Demographics: {'yes' if item.demographics else 'no'}")
                self._report_progress(f"Proximity metrics: {len(item.proximity)}")
                self._report_progress("=" * 80 + "\n")

            if self.show_thinking:
                self._report_progress("\n" + "-" * 80)
                self._report_progress("ENRICHMENT DETAILS:")
                self._report_progress("-" * 80)
                self._report_progress(f"Name: {item.name}")
                self._report_progress(f"City: {item.city or 'unknown'}")
                self._report_progress(f"County: {item.county or 'unknown'}")
                self._report_progress(f"Gated: {item.is_gated}")
                self._report_progress(f"Confidence: {item.confidence:.2f}")
                if item.overview:
                    self._report_progress(f"Overview: {item.overview}")
                
                if item.fees:
                    self._report_progress("\nFees:")
                    for fee in item.fees:
                        self._report_progress(f"  - {fee.fee_type}: ${fee.amount or 'unknown'} ({fee.period or 'unknown'})")
                
                if item.amenities:
                    self._report_progress("\nAmenities:")
                    for amenity in item.amenities:
                        detail = f" ({amenity.detail})" if amenity.detail else ""
                        self._report_progress(f"  - {amenity.amenity}{detail}")
                
                if item.demographics:
                    self._report_progress("\nDemographics:")
                    if item.demographics.median_age:
                        self._report_progress(f"  - Median age: {item.demographics.median_age}")
                    if item.demographics.median_household_income:
                        self._report_progress(f"  - Median income: ${item.demographics.median_household_income:,.0f}")
                    if item.demographics.owner_occupancy_pct:
                        self._report_progress(f"  - Owner occupancy: {item.demographics.owner_occupancy_pct}%")
                    if item.demographics.population:
                        self._report_progress(f"  - Population: {item.demographics.population}")
                
                if item.proximity:
                    self._report_progress("\nProximity:")
                    for prox in item.proximity:
                        self._report_progress(f"  - {prox.category}: {prox.nearest_name or 'unknown'} ({prox.distance_miles or '?'} miles)")
                
                self._report_progress("-" * 80 + "\n")

            self._normalize_source_urls(item)
            self._report_progress(f"Successfully enriched {item.name} in {elapsed:.2f}s")
            self._report_enrichment(item)
            return item
        except Exception as exc:
            logger.error("Enrichment failed for %s: %s", name, exc)
            self._report_progress(f"ERROR: Enrichment failed for {name}: {exc}")
            raise

    def _normalize_source_urls(self, item: CondensedCommunityItem) -> None:
        for fee in item.fees:
            if not fee.source_url.startswith(("http://", "https://", "ai:")):
                fee.source_url = AI_SOURCE_URL
        for amenity in item.amenities:
            if not amenity.source_url.startswith(("http://", "https://", "ai:")):
                amenity.source_url = AI_SOURCE_URL
        if item.demographics is not None:
            if not item.demographics.source_url.startswith(("http://", "https://", "ai:")):
                item.demographics.source_url = AI_SOURCE_URL
        for prox in item.proximity:
            if not prox.source_url.startswith(("http://", "https://", "ai:")):
                prox.source_url = AI_SOURCE_URL

    def _save_item(self, item: CondensedCommunityItem) -> str:
        record = item.to_record()
        self.store.save_community(record)
        self.database.upsert_record(record, data_source="ai_condenser")
        return record.identity.slug

    async def run_discover(self, limit: int = 100) -> list[CondenserResult]:
        items = await self.discover_communities(limit=limit)
        results: list[CondenserResult] = []
        for item in items:
            slug = self._save_item(item)
            results.append(CondenserResult(
                community=item.name,
                slug=slug,
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
        limit: int = 50,
    ) -> list[CondenserResult]:
        if communities is None:
            targets = self.store.load_target_list()
            communities = [t["name"] for t in targets[:limit]]

        results: list[CondenserResult] = []
        for name in communities:
            started = time.monotonic()
            report = CondenserResult(community=name, slug=slugify(name))
            try:
                seed = next(
                    (t for t in self.store.load_target_list() if t["name"].lower() == name.lower()),
                    None,
                )
                city = seed.get("city") if seed else None
                item = await self.enrich_community(name, city=city)
                slug = self._save_item(item)
                report.slug = slug
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
            logger.info("Enriched %s: %d fees, %d amenities", name, report.fees, report.amenities)
        return results

    async def run_full(self, limit: int = 200) -> list[CondenserResult]:
        discover_results = await self.run_discover(limit=limit)
        logger.info("Discovery complete: %d communities", len(discover_results))
        return discover_results

    def get_summary(self) -> dict[str, Any]:
        return self.database.summary()


async def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="AI Condenser — quickly populate community database from AI knowledge."
    )
    parser.add_argument(
        "--mode",
        choices=["discover", "enrich", "full", "summary"],
        default="summary",
        help="Operation mode: discover (list communities), enrich (add details), full (discover+enrich), summary (show DB stats)",
    )
    parser.add_argument("--limit", type=int, default=100, help="Max communities to process")
    parser.add_argument("--community", help="Enrich a specific community by name")
    parser.add_argument("--db-path", default="data/communities.db", help="SQLite database path")
    parser.add_argument("--verbose", action="store_true", help="Show prompts, responses, and parsed output")
    parser.add_argument("--show-thinking", action="store_true", help="Show the AI's reasoning process and detailed results")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Progress callbacks for CLI output
    def on_progress(message: str) -> None:
        print(f"[PROGRESS] {message}")

    def on_discovery(item: CondensedCommunityItem) -> None:
        print(f"[DISCOVERED] {item.name} ({item.city or 'unknown location'})")

    def on_enrichment(item: CondensedCommunityItem) -> None:
        print(f"[ENRICHED] {item.name}: {len(item.fees)} fees, {len(item.amenities)} amenities")

    database = CommunityDatabase(args.db_path)
    database.create_tables()
    condenser = AICondenser(
        database=database,
        verbose=args.verbose,
        show_thinking=args.show_thinking,
        on_progress=on_progress,
        on_discovery=on_discovery,
        on_enrichment=on_enrichment,
    )

    if args.verbose or args.show_thinking:
        print(f"\n[CONFIG] Model: {condenser.model_name}")
        print(f"[CONFIG] Base URL: {condenser.base_url}")
        print(f"[CONFIG] Verbose: {args.verbose}")
        print(f"[CONFIG] Show thinking: {args.show_thinking}\n")

    if args.mode == "summary":
        summary = condenser.get_summary()
        print(json.dumps(summary, indent=2))
        return

    if args.community:
        started = time.monotonic()
        item = await condenser.enrich_community(args.community)
        slug = condenser._save_item(item)
        elapsed = time.monotonic() - started
        print(json.dumps({
            "community": args.community,
            "slug": slug,
            "fees": len(item.fees),
            "amenities": len(item.amenities),
            "proximity": len(item.proximity),
            "demographics_present": item.demographics is not None,
            "elapsed_seconds": round(elapsed, 2),
        }, indent=2))
        return

    if args.mode == "discover":
        results = await condenser.run_discover(limit=args.limit)
    elif args.mode == "enrich":
        results = await condenser.run_enrich(limit=args.limit)
    elif args.mode == "full":
        results = await condenser.run_full(limit=args.limit)
    else:
        return

    for result in results:
        print(json.dumps(result.to_dict()))

    summary = condenser.get_summary()
    print(f"\n{len(results)} community/communities processed")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
