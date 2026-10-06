"""Browser-driven community data condenser with AI gap analysis.

Uses Playwright to automate Chrome/Edge for Google searches, then uses the local
model to extract structured community data from search results. Iteratively analyzes
gaps in collected data and generates targeted queries to fill those gaps.

Priority: Data completeness (fees, amenities, demographics) > Geographic coverage (all counties)
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from .database import CommunityDatabase
from .models import (
    AmenityFact,
    BROWSER_SOURCE_URL,
    BrowserCondenserState,
    CommunityFacts,
    CommunityIdentity,
    CommunityRecord,
    CondensedCommunityItem,
    Demographics,
    FeeFact,
    FLORIDA_COUNTIES,
    GapAnalysis,
    GoogleSearchResult,
    NextQuery,
    ProximityMetric,
)
from .store import CommunityStore, slugify
from .tools import google_search, read_page

logger = logging.getLogger(__name__)


class BrowserCondenserConfig(BaseModel):
    """Configuration for the browser condenser."""

    max_iterations: int = Field(default=20, description="Maximum iterations before stopping")
    max_results_per_query: int = Field(default=10, description="Max search results per query")
    save_interval: int = Field(default=1, description="Save state every N iterations")
    chrome_profile_path: str | None = Field(
        default=None, description="Path to Chrome profile directory"
    )
    headless: bool = Field(default=False, description="Run browser in headless mode")
    delay_between_searches: float = Field(
        default=2.0, description="Delay between searches (seconds)"
    )
    delay_between_page_reads: float = Field(
        default=1.0, description="Delay between page reads (seconds)"
    )
    max_pages_per_query: int = Field(
        default=3, description="Max pages to read per query"
    )


class BrowserCondenser:
    """AI-driven community data accumulator using browser automation.

    Workflow:
    1. Analyze gaps in current data
    2. Generate targeted query based on gaps
    3. Search Google via browser automation
    4. Read top results and extract data using local model
    5. Save extracted data
    6. Update state and repeat

    The AI prioritizes filling data gaps (fees, amenities, demographics) over
    geographic coverage, but tracks county-level gaps for full Florida coverage.
    """

    def __init__(
        self,
        config: BrowserCondenserConfig | None = None,
        store: CommunityStore | None = None,
        database: CommunityDatabase | None = None,
        state_path: Path | None = None,
    ):
        self.config = config or BrowserCondenserConfig()
        self.store = store or CommunityStore()
        self.database = database or CommunityDatabase()
        self.state_path = state_path or Path("data/communities/browser_condenser_state.json")
        self.state = self._load_state()

        self._model = None
        self._iteration = 0
        self._communities_found_this_run = 0

    def _load_state(self) -> BrowserCondenserState:
        """Load persisted state from disk."""
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                return BrowserCondenserState.model_validate(data)
            except Exception as e:
                logger.warning(f"Failed to load state from {self.state_path}: {e}")
        return BrowserCondenserState()

    def _save_state(self):
        """Persist current state to disk."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            self.state.model_dump_json(indent=2), encoding="utf-8"
        )

    def _get_model(self):
        """Get or create the local model."""
        if self._model is None:
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider
            
            # Always use local model - no external API needed
            self._model = OpenAIChatModel(
                "qwen3.8-27b",
                provider=OpenAIProvider(
                    base_url="http://localhost:8080/v1",
                    api_key="not-needed",
                ),
            )
        return self._model

    async def analyze_gaps(self) -> GapAnalysis:
        """Analyze gaps in current community data.

        Reviews what data we have and identifies what's missing.
        Prioritizes data completeness over geographic coverage.
        """
        logger.info("Analyzing data gaps...")

        communities = []
        for record in self.database.list_records():
            communities.append({
                "name": record.identity.name,
                "county": record.identity.county_fips,
                "has_fees": len(record.fees) > 0,
                "has_amenities": len(record.amenities) > 0,
                "has_demographics": record.demographics is not None,
                "has_proximity": len(record.proximity) > 0,
            })

        county_coverage = {}
        for county in FLORIDA_COUNTIES:
            county_communities = [c for c in communities if c.get("county") == county]
            county_coverage[county] = len(county_communities)

        prompt = f"""Analyze the gaps in our community data and prioritize what to collect next.

Current data:
{json.dumps(communities, indent=2)}

Florida county coverage:
{json.dumps(county_coverage, indent=2)}

Your task:
1. Identify communities missing critical data (fees > amenities > demographics > proximity)
2. Identify counties with few or no communities
3. Prioritize: data completeness first, then geographic coverage
4. Return a structured analysis with the most critical gap to fill next

Priority order:
- Missing fees (most important)
- Missing amenities
- Missing demographics
- Missing proximity
- Counties with < 3 communities (geographic gap)

Be specific: name the exact community or county to target next."""

        agent = Agent(
            self._get_model(),
            output_type=GapAnalysis,
            system_prompt="You are a data analyst specializing in real estate community data. Be specific and actionable.",
        )

        result = await agent.run(prompt)
        gap_analysis = result.output

        logger.info(
            f"Gap analysis complete: priority={gap_analysis.priority_gap}, "
            f"target={gap_analysis.priority_target}"
        )
        return gap_analysis

    async def generate_query(self, gap_analysis: GapAnalysis) -> NextQuery:
        """Generate a targeted Google search query based on gap analysis.

        Creates a query designed to fill the identified gap.
        """
        logger.info(f"Generating query for gap: {gap_analysis.priority_gap}")

        prompt = f"""Generate a Google search query to fill this data gap:

Gap type: {gap_analysis.priority_gap}
Priority target: {gap_analysis.priority_target}
Reasoning: {gap_analysis.reasoning}

Already executed queries:
{chr(10).join(f"- {q}" for q in self.state.queries_executed[-10:])}

Generate a Google search query that will help us fill this gap. Be specific and targeted.

Query types:
- discover: Find new communities (e.g., "gated communities in Miami-Dade County")
- enrich: Find specific data for a community (e.g., "Pelican Bay HOA fees amenities")
- geographic: Find communities in underrepresented counties (e.g., "gated communities Sarasota County Florida")

Return the query and explain what you expect to find."""

        agent = Agent(
            self._get_model(),
            output_type=NextQuery,
            system_prompt="You are a search expert specializing in Florida real estate data. Create effective, targeted queries.",
        )

        result = await agent.run(prompt)
        next_query = result.output

        logger.info(f"Generated query: {next_query.query} (type: {next_query.target_type})")
        return next_query

    async def execute_query(self, query: str) -> list[GoogleSearchResult]:
        """Execute a Google search query via browser automation.

        Returns list of search results.
        """
        logger.info(f"Executing search: {query}")

        results = await google_search(
            query=query,
            max_results=self.config.max_results_per_query,
            profile_path=self.config.chrome_profile_path,
            headless=self.config.headless,
            delay_seconds=self.config.delay_between_searches,
        )

        logger.info(f"Found {len(results)} search results")
        return results

    async def read_and_extract(self, search_results: list[GoogleSearchResult]) -> list[CondensedCommunityItem]:
        """Read pages from search results and extract community data.

        Uses the local model to extract structured data from page content.
        """
        extracted_communities = []

        for result in search_results[: self.config.max_pages_per_query]:
            logger.info(f"Reading: {result.url}")

            await self._async_sleep(self.config.delay_between_page_reads)

            page_content = await read_page(
                url=result.url,
                max_chars=8000,
                use_js=True,
            )

            if not page_content:
                logger.warning(f"Could not read page: {result.url}")
                continue

            logger.info(f"Extracting data from: {result.title}")

            prompt = f"""Extract information about gated communities from this page.

Page title: {result.title}
URL: {result.url}
Content:
{page_content}

Extract all gated communities mentioned with:
- Name
- City
- County (if mentioned)
- HOA fees (monthly or annual)
- Amenities (list)
- Demographics (median age, income, population if available)
- Nearby facilities (shopping, hospitals, schools)

If no gated communities are mentioned, return an empty list.
Be accurate - only extract data that is explicitly stated."""

            agent = Agent(
                self._get_model(),
                output_type=ExtractedCommunities,
                system_prompt="You are a real estate data extraction specialist. Extract only what is explicitly stated. Be accurate.",
            )

            try:
                result_obj = await agent.run(prompt)
                extracted = result_obj.output

                for item in extracted.communities:
                    item.source_url = result.url
                    item.extraction_method = "browser_google"
                    extracted_communities.append(item)
                    self._communities_found_this_run += 1

            except Exception as e:
                logger.error(f"Failed to extract data from {result.url}: {e}")

        logger.info(f"Extracted {len(extracted_communities)} communities from pages")
        return extracted_communities

    async def save_results(self, communities: list[CondensedCommunityItem]):
        """Save extracted communities to store and database."""
        for item in communities:
            record = item.to_record()
            self.store.save_community(record)
            self.database.upsert_record(record, data_source="browser_condenser")

        logger.info(f"Saved {len(communities)} communities to store and database")

    def update_state(self, query: NextQuery, gap_analysis: GapAnalysis):
        """Update the persistent state after an iteration."""
        self.state.queries_executed.append(query.query)
        self.state.last_gap_analysis = gap_analysis
        self.state.last_query = query
        self.state.total_iterations += 1

        if self._iteration % self.config.save_interval == 0:
            self._save_state()

    async def run_loop(self):
        """Main accumulation loop.

        Iterates: analyze gaps -> generate query -> execute -> extract -> save
        Continues until max_iterations or no gaps remain.
        """
        logger.info(f"Starting browser condenser loop (max {self.config.max_iterations} iterations)")

        for iteration in range(self.config.max_iterations):
            self._iteration = iteration + 1
            logger.info(f"\n=== Iteration {self._iteration}/{self.config.max_iterations} ===")

            gap_analysis = await self.analyze_gaps()

            if not gap_analysis.has_any_gaps:
                logger.info("No significant gaps found. Stopping.")
                self.state.stopped_reason = "no_gaps"
                break

            next_query = await self.generate_query(gap_analysis)

            if next_query.query in self.state.queries_executed:
                logger.warning(f"Query already executed: {next_query.query}. Generating alternative.")
                next_query.query = f"{next_query.query} Florida"

            search_results = await self.execute_query(next_query.query)

            if not search_results:
                logger.warning("No search results. Continuing to next iteration.")
                continue

            extracted = await self.read_and_extract(search_results)

            if extracted:
                await self.save_results(extracted)

            self.update_state(next_query, gap_analysis)

            logger.info(
                f"Iteration {self._iteration} complete. "
                f"Total communities found: {self._communities_found_this_run}"
            )

        if self._iteration >= self.config.max_iterations:
            self.state.stopped_reason = "max_iterations"

        self._save_state()
        logger.info(f"\nBrowser condenser loop complete. Found {self._communities_found_this_run} communities.")

    async def _async_sleep(self, seconds: float):
        """Async sleep helper."""
        import asyncio
        await asyncio.sleep(seconds)


class ExtractedCommunities(BaseModel):
    """Output model for extracting communities from a page."""

    communities: list[CondensedCommunityItem] = Field(
        default_factory=list, description="List of communities found on the page"
    )
    page_summary: str = Field(default="", description="Brief summary of what the page is about")


async def main():
    """CLI entry point for browser condenser."""
    import argparse

    parser = argparse.ArgumentParser(description="Browser-driven community data condenser")
    parser.add_argument("--max-iterations", type=int, default=20, help="Maximum iterations")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--chrome-profile", type=str, help="Path to Chrome profile directory")
    parser.add_argument("--save-interval", type=int, default=1, help="Save state every N iterations")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = BrowserCondenserConfig(
        max_iterations=args.max_iterations,
        headless=args.headless,
        chrome_profile_path=args.chrome_profile,
        save_interval=args.save_interval,
    )

    condenser = BrowserCondenser(config=config)
    await condenser.run_loop()

    summary = condenser.database.summary()
    print("\n" + "=" * 80)
    print("BROWSER CONDENSER SUMMARY")
    print("=" * 80)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
