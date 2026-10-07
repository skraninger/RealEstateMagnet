"""Web-based AI condenser — uses web search + local model to accumulate community data.

This module implements a hybrid approach:
1. Uses DuckDuckGo web search (like Gemini's web search capability)
2. Local model reads the search results and pages
3. Extracts structured community data
4. Loops through queries to accumulate comprehensive data

This avoids needing a Gemini API key while still leveraging web-based AI research.

Usage::

    python -m modules.community.web_condenser --mode discover --limit 50
    python -m modules.community.web_condenser --mode enrich --community "Pelican Bay"
    python -m modules.community.web_condenser --mode accumulate --limit 100 --verbose
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

from .database import CommunityDatabase
from .models import (
    AI_SOURCE_URL,
    AmenityFact,
    CommunityFacts,
    CommunityIdentity,
    CommunityRecord,
    CondensedCommunityBatch,
    CondensedCommunityItem,
    Demographics,
    FeeFact,
    ProximityMetric,
    utcnow,
)
from .store import CommunityStore, merge_facts, slugify
from .tools import read_page, web_search

logger = logging.getLogger(__name__)

WEB_DISCOVERY_SYSTEM_PROMPT = """\
You are analyzing web search results to identify gated communities in southern Florida.

From the search results provided, extract communities mentioned with their details:
- Full community name
- City/location
- County
- Whether it is gated
- Brief overview (1-2 sentences)
- Your confidence level (0.0 to 1.0)

Focus on well-known communities. Include confidence scores based on how clearly the results \
identify them as gated communities.

Only include residential communities in southern Florida (Miami-Dade, Broward, Palm Beach, \
Collier, Martin counties). Do not include apartment complexes unless they are clearly gated \
communities with HOA.
"""

WEB_ENRICHMENT_SYSTEM_PROMPT = """\
You are a real-estate expert analyzing web content about a specific gated community.

From the web content provided, extract all available information about:

1. **Fees** — HOA dues (monthly/annual amounts), CDD special assessments, other recurring charges.
2. **Amenities** — pool, clubhouse, golf, tennis, pickleball, trails, fitness center, gate/security, etc.
3. **Demographics** — median age, median household income, owner-occupancy %, population.
4. **Proximity** — nearest shopping, library, hospital, school with approximate distances.

Rules:
- Only extract facts that are explicitly stated in the content.
- Use 'web:extracted' as the source_url for all facts (indicates web-extracted data).
- Set confidence based on source quality: 0.8+ for official sites, 0.5-0.8 for reputable sources, below 0.5 for forums/unverified.
- If information is not found in the content, leave fields null/empty.
- For fees, report exact amounts if stated, otherwise note the uncertainty.
"""


@dataclass
class WebCondenserResult:
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


class WebAICondenser:
    """Uses web search + local model to accumulate community data from the web.

    This approach:
    1. Uses DuckDuckGo web search (free, no API key)
    2. Local model reads and analyzes the search results
    3. Extracts structured community data
    4. Loops through queries to accumulate comprehensive data

    Similar to how Gemini works in a browser, but using the local model + web search.
    """

    def __init__(
        self,
        model: Any | None = None,
        model_name: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        max_tokens: int | None = None,
        store: CommunityStore | None = None,
        database: CommunityDatabase | None = None,
        url_tracker: Any | None = None,
        verbose: bool = False,
        show_thinking: bool = False,
        on_progress: Optional[Callable[[str], None]] = None,
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
        self.store = store or CommunityStore()
        self.database = database or CommunityDatabase()
        self.url_tracker = url_tracker
        self.verbose = verbose
        self.show_thinking = show_thinking
        self.on_progress = on_progress
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

    async def discover_from_web(
        self,
        query: str = "gated communities in southern Florida",
        limit: int = 20,
    ) -> list[CondensedCommunityItem]:
        """Use web search + local model to discover communities from the web."""
        self._report_progress(f"Searching web for: {query}")
        
        # Step 1: Search the web
        search_results = web_search(query, max_results=limit)
        if not search_results:
            self._report_progress("No web search results found")
            return []
        
        self._report_progress(f"Found {len(search_results)} web search results")
        
        # Register URLs immediately when discovered
        if self.url_tracker:
            urls = [sr.url for sr in search_results]
            self.url_tracker.register_urls(urls, discovered_by="web_condenser")
        
        # Step 2: Read the actual pages to get community names
        page_contents = []
        for sr in search_results[:5]:  # Read top 5 pages
            self._report_progress(f"Reading: {sr.url}")
            page_text = await read_page(sr.url, max_chars=6000)
            if page_text:
                page_contents.append(f"Source: {sr.title}\nURL: {sr.url}\n\n{page_text}")
                # Update URL with quality score after successful read
                if self.url_tracker:
                    from .url_tracker import calculate_data_quality
                    quality_metrics = calculate_data_quality(page_text)
                    self.url_tracker.update_url_quality(
                        sr.url,
                        has_community_data=quality_metrics["has_community_data"],
                        data_quality_score=quality_metrics["data_quality_score"],
                        data_types_found=quality_metrics["data_types_found"],
                        data_summary=quality_metrics["data_summary"],
                        status="printed",
                    )
        
        if not page_contents:
            self._report_progress("Could not read any pages")
            return []
        
        self._report_progress(f"Read {len(page_contents)} pages successfully")
        
        # Step 3: Compile page contents for the model
        combined_content = "\n\n" + "=" * 80 + "\n\n".join(page_contents)
        
        # Step 4: Have the local model analyze the page content
        agent = Agent(
            self._build_model(),
            output_type=CondensedCommunityBatch,
            system_prompt=WEB_DISCOVERY_SYSTEM_PROMPT,
            retries=2,
        )
        
        prompt = (
            f"Analyze these web pages and extract gated communities:\n\n"
            f"{combined_content[:8000]}"
        )
        
        if self.verbose:
            self._report_progress("\n" + "=" * 80)
            self._report_progress("PROMPT TO LOCAL MODEL:")
            self._report_progress("=" * 80)
            self._report_progress(prompt[:1000] + "...")
            self._report_progress("=" * 80 + "\n")
        
        started = time.monotonic()
        result = await agent.run(prompt, model_settings={"max_tokens": self.max_tokens})
        elapsed = time.monotonic() - started
        batch: CondensedCommunityBatch = result.output
        
        if self.verbose:
            self._report_progress(f"Local model extracted {len(batch.communities)} communities in {elapsed:.2f}s")
        
        if self.show_thinking:
            self._report_progress("\n" + "-" * 80)
            self._report_progress("COMMUNITIES EXTRACTED FROM WEB:")
            self._report_progress("-" * 80)
            for i, comm in enumerate(batch.communities, 1):
                self._report_progress(f"{i}. {comm.name} ({comm.city or 'unknown'})")
                if comm.overview:
                    self._report_progress(f"   {comm.overview[:150]}...")
            self._report_progress("-" * 80 + "\n")
        
        for comm in batch.communities:
            self._report_discovery(comm)
        
        return batch.communities

    async def enrich_from_web(self, name: str, city: Optional[str] = None) -> CondensedCommunityItem:
        """Use web search + local model to enrich a community from web content."""
        location = f" {city}" if city else ""
        community_slug = slugify(name)
        self._report_progress(f"Searching web for: {name}{location}")
        
        # Step 1: Search the web
        queries = [
            f"{name}{location} gated community HOA fees",
            f"{name}{location} amenities",
            f"{name}{location} demographics",
        ]
        
        all_content = []
        sources_consulted = 0
        
        for query in queries:
            search_results = web_search(query, max_results=5)
            # Register URLs immediately when discovered
            if self.url_tracker:
                urls = [sr.url for sr in search_results]
                self.url_tracker.register_urls(
                    urls, discovered_by="web_condenser", community_slug=community_slug
                )
            
            for sr in search_results[:2]:
                # Read the page content
                page_text = await read_page(sr.url, max_chars=8000)
                if page_text:
                    all_content.append(f"Source: {sr.url}\n{page_text[:2000]}")
                    sources_consulted += 1
                    # Update URL with quality score after successful read
                    if self.url_tracker:
                        from .url_tracker import calculate_data_quality
                        quality_metrics = calculate_data_quality(page_text)
                        self.url_tracker.update_url_quality(
                            sr.url,
                            has_community_data=quality_metrics["has_community_data"],
                            data_quality_score=quality_metrics["data_quality_score"],
                            data_types_found=quality_metrics["data_types_found"],
                            data_summary=quality_metrics["data_summary"],
                            status="printed",
                            community_slug=community_slug,
                        )
        
        if not all_content:
            self._report_progress(f"No web content found for {name}")
            return CondensedCommunityItem(name=name, city=city)
        
        self._report_progress(f"Read {sources_consulted} web pages about {name}")
        
        # Step 2: Have the local model analyze the web content
        agent = Agent(
            self._build_model(),
            output_type=CondensedCommunityItem,
            system_prompt=WEB_ENRICHMENT_SYSTEM_PROMPT,
            retries=2,
        )
        
        content_text = "\n\n---\n\n".join(all_content)
        prompt = (
            f"Extract detailed information about {name} from this web content:\n\n"
            f"{content_text[:6000]}"
        )
        
        if self.verbose:
            self._report_progress("\n" + "=" * 80)
            self._report_progress("PROMPT TO LOCAL MODEL:")
            self._report_progress("=" * 80)
            self._report_progress(prompt[:500] + "...")
            self._report_progress("=" * 80 + "\n")
        
        started = time.monotonic()
        result = await agent.run(prompt, model_settings={"max_tokens": self.max_tokens})
        elapsed = time.monotonic() - started
        item: CondensedCommunityItem = result.output
        
        # Mark source as web-extracted
        for fee in item.fees:
            if not fee.source_url.startswith(("http://", "https://", "ai:")):
                fee.source_url = "web:extracted"
        for amenity in item.amenities:
            if not amenity.source_url.startswith(("http://", "https://", "ai:")):
                amenity.source_url = "web:extracted"
        if item.demographics:
            if not item.demographics.source_url.startswith(("http://", "https://", "ai:")):
                item.demographics.source_url = "web:extracted"
        for prox in item.proximity:
            if not prox.source_url.startswith(("http://", "https://", "ai:")):
                prox.source_url = "web:extracted"
        
        if self.verbose:
            self._report_progress(f"Local model enriched {name} in {elapsed:.2f}s")
            self._report_progress(f"Extracted: {len(item.fees)} fees, {len(item.amenities)} amenities")
        
        if self.show_thinking:
            self._report_progress("\n" + "-" * 80)
            self._report_progress("EXTRACTED DETAILS:")
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
                self._report_progress(f"Demographics: population={item.demographics.population}, median_age={item.demographics.median_age}")
            self._report_progress("-" * 80 + "\n")
        
        self._report_enrichment(item)
        return item

    def _save_item(self, item: CondensedCommunityItem) -> str:
        record = item.to_record()
        self.store.save_community(record)
        self.database.upsert_record(record, data_source="web_condenser")
        return record.identity.slug

    async def run_discover(self, limit: int = 50) -> list[WebCondenserResult]:
        """Discover communities from web search."""
        queries = [
            "gated communities in Miami-Dade County Florida",
            "gated communities in Broward County Florida",
            "gated communities in Palm Beach County Florida",
            "luxury gated communities south florida",
            "55+ active adult communities florida",
        ]
        
        all_items: list[CondensedCommunityItem] = []
        results: list[WebCondenserResult] = []
        
        for query in queries:
            if len(all_items) >= limit:
                break
            items = await self.discover_from_web(query, limit=min(20, limit - len(all_items)))
            all_items.extend(items)
        
        # Deduplicate by name
        seen = set()
        for item in all_items:
            if item.name.lower() not in seen:
                seen.add(item.name.lower())
                slug = self._save_item(item)
                results.append(WebCondenserResult(
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
        limit: int = 20,
    ) -> list[WebCondenserResult]:
        """Enrich communities from web search."""
        if communities is None:
            targets = self.store.load_target_list()
            communities = [t["name"] for t in targets[:limit]]
        
        results: list[WebCondenserResult] = []
        for name in communities:
            started = time.monotonic()
            report = WebCondenserResult(community=name, slug=slugify(name))
            try:
                seed = next(
                    (t for t in self.store.load_target_list() if t["name"].lower() == name.lower()),
                    None,
                )
                city = seed.get("city") if seed else None
                item = await self.enrich_from_web(name, city=city)
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
        
        return results

    async def run_accumulate(self, limit: int = 100) -> list[WebCondenserResult]:
        """Discover + enrich in a loop to accumulate comprehensive data."""
        self._report_progress("Starting accumulation loop: discover + enrich")
        
        # Phase 1: Discover communities
        discover_results = await self.run_discover(limit=limit)
        self._report_progress(f"Discovered {len(discover_results)} communities from web")
        
        # Phase 2: Enrich discovered communities
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
        description="Web-based AI Condenser — accumulate community data from web search + local model."
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
    condenser = WebAICondenser(
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
        item = await condenser.enrich_from_web(args.community)
        slug = condenser._save_item(item)
        print(json.dumps({
            "community": args.community,
            "slug": slug,
            "fees": len(item.fees),
            "amenities": len(item.amenities),
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
