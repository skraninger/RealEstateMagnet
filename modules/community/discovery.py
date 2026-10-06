"""Community discovery engine — uses the local LLM to discover gated communities.

The discovery process:
1. Search for "south florida gated communities" and similar queries
2. Read directory/listing pages
3. Use the LLM to extract community names with structured output
4. Verify each candidate using the LLM
5. Output a verified target list for the research agent

This replaces regex-based extraction with LLM-powered understanding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Callable

from pydantic_ai import Agent

from .models import (
    DiscoveredCommunityItem,
    PageExtractionResult,
    VerificationResult,
)
from .tools import read_page, web_search, take_screenshot, analyze_screenshot

logger = logging.getLogger(__name__)

DEFAULT_SEED_QUERIES = [
    "south florida gated communities list",
    "miami-dade county gated communities",
    "broward county gated communities",
    "palm beach county gated communities",
    "florida 55+ active adult communities",
    "florida master planned communities",
    "hoa communities south florida",
    "golf course communities south florida",
]

TARGET_COUNTIES = ["miami-dade", "broward", "palm beach", "collier", "martin"]

EXTRACTION_SYSTEM_PROMPT = """\
You are analyzing web pages to find gated communities in southern Florida.

For each page, identify any communities mentioned. Focus on:
- Community names (e.g., "The Villages", "Pelican Bay", "Sunset Ridge Estates")
- Whether they are gated or have an HOA
- Their location (city, county)
- Evidence from the page text

Only include communities that appear to be real residential communities, not:
- Commercial properties or shopping centers
- Apartment complexes (unless they are gated communities)
- Individual homes for sale (unless part of a named community)
- Communities outside southern Florida (Miami-Dade, Broward, Palm Beach, Collier, Martin counties)

Be thorough but accurate. Include confidence scores based on how clearly the page \
identifies them as gated communities.
"""

VERIFICATION_SYSTEM_PROMPT = """\
You are verifying whether a community candidate is a real gated community in southern Florida.

Given the community name and evidence from web searches, determine:
1. Is this a real residential community (not a business, park, or other non-residential place)?
2. Is it gated or does it have an HOA?
3. Is it located in southern Florida (Miami-Dade, Broward, Palm Beach, Collier, or Martin county)?

If the name seems wrong or there's a more common name, provide the correction.
Be conservative — only confirm communities you're confident are real and in the target area.
"""


@dataclass
class DiscoveredCommunity:
    """A community found during discovery."""

    name: str
    source_url: str
    source_name: str
    city: Optional[str] = None
    county: Optional[str] = None
    confidence: float = 0.5
    is_gated: bool = True
    evidence: str = ""
    verified: bool = False
    verification_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_url": self.source_url,
            "source_name": self.source_name,
            "city": self.city,
            "county": self.county,
            "confidence": self.confidence,
            "is_gated": self.is_gated,
            "evidence": self.evidence,
            "verified": self.verified,
            "verification_notes": self.verification_notes,
        }

    @classmethod
    def from_llm_item(
        cls,
        item: DiscoveredCommunityItem,
        source_url: str,
        source_name: str,
    ) -> DiscoveredCommunity:
        return cls(
            name=item.name,
            source_url=source_url,
            source_name=source_name,
            city=item.city,
            county=item.county,
            confidence=item.confidence,
            is_gated=item.is_gated,
            evidence=item.evidence,
        )


@dataclass
class DiscoveryState:
    """Persistent state for resumable discovery."""

    discovered_communities: list[dict] = field(default_factory=list)
    verified_communities: list[dict] = field(default_factory=list)
    processed_urls: list[str] = field(default_factory=list)
    processed_queries: list[str] = field(default_factory=list)
    consecutive_empty_pages: int = 0
    stopped_reason: Optional[str] = None

    def save(self, path: Path) -> None:
        """Save state to disk."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "discovered_communities": self.discovered_communities,
            "verified_communities": self.verified_communities,
            "processed_urls": self.processed_urls,
            "processed_queries": self.processed_queries,
            "consecutive_empty_pages": self.consecutive_empty_pages,
            "stopped_reason": self.stopped_reason,
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> DiscoveryState:
        """Load state from disk."""
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                discovered_communities=data.get("discovered_communities", []),
                verified_communities=data.get("verified_communities", []),
                processed_urls=data.get("processed_urls", []),
                processed_queries=data.get("processed_queries", []),
                consecutive_empty_pages=data.get("consecutive_empty_pages", 0),
                stopped_reason=data.get("stopped_reason"),
            )
        except Exception as exc:
            logger.warning("Failed to load state from %s: %s", path, exc)
            return cls()

    def has_discovered(self, name: str) -> bool:
        """Check if a community has already been discovered."""
        normalized = name.lower().strip()
        return any(c["name"].lower().strip() == normalized for c in self.discovered_communities)

    def has_processed_url(self, url: str) -> bool:
        """Check if a URL has already been processed."""
        return url in self.processed_urls

    def has_processed_query(self, query: str) -> bool:
        """Check if a query has already been processed."""
        return query in self.processed_queries


@dataclass
class DiscoveryResult:
    """Result of a discovery run."""

    communities: list[DiscoveredCommunity] = field(default_factory=list)
    sources_consulted: list[str] = field(default_factory=list)
    queries_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "communities": [c.to_dict() for c in self.communities],
            "sources_consulted": self.sources_consulted,
            "queries_used": self.queries_used,
            "errors": self.errors,
            "total_discovered": len(self.communities),
            "verified_count": sum(1 for c in self.communities if c.verified),
        }


class CommunityDiscoveryEngine:
    """Discovers gated communities using the local LLM for extraction."""

    def __init__(
        self,
        seed_queries: Optional[list[str]] = None,
        max_results_per_query: int = 10,
        page_max_chars: int = 15000,
        model: Any | None = None,
        model_name: str | None = None,
        base_url: str | None = None,
        max_tokens: int | None = None,
        state_path: Optional[Path] = None,
        max_communities: int = 200,
        dead_end_threshold: int = 10,
        on_discovery: Optional[Callable[[DiscoveredCommunity], None]] = None,
        on_verification: Optional[Callable[[DiscoveredCommunity], None]] = None,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.seed_queries = seed_queries or DEFAULT_SEED_QUERIES
        self.max_results_per_query = max_results_per_query
        self.page_max_chars = page_max_chars
        self.model = model
        self.model_name = model_name or os.environ.get("MODEL_NAME", "local")
        self.base_url = base_url or os.environ.get(
            "MODEL_BASE_URL", "http://localhost:8080/v1"
        )
        self.max_tokens = max_tokens or int(os.environ.get("MODEL_MAX_TOKENS", "8192"))
        
        # Resumable state
        self.state_path = state_path or Path("data/communities/discovery_state.json")
        self.state = DiscoveryState.load(self.state_path)
        
        # Stop conditions
        self.max_communities = max_communities
        self.dead_end_threshold = dead_end_threshold
        
        # Progress callbacks
        self.on_discovery = on_discovery
        self.on_verification = on_verification
        self.on_progress = on_progress

    def _build_model(self) -> Any:
        if self.model is not None:
            return self.model
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        return OpenAIChatModel(
            self.model_name,
            provider=OpenAIProvider(
                base_url=self.base_url,
                api_key=os.environ.get("MODEL_API_KEY", "local"),
            ),
        )

    async def extract_communities_from_page(
        self,
        page_text: str,
        source_url: str,
        source_name: str,
        use_vision: bool = False,
    ) -> list[DiscoveredCommunity]:
        """Use the LLM to extract community names from a page.
        
        Args:
            page_text: Text content of the page
            source_url: URL of the page
            source_name: Name/title of the page
            use_vision: If True, use vision capabilities even if text is available
        """
        # If text is insufficient and we have vision capability, use vision
        if (not page_text or len(page_text.strip()) < 100) and not use_vision:
            logger.info("Text extraction insufficient for %s, attempting vision analysis", source_url)
            return await self.extract_communities_from_screenshot(source_url, source_name)
        
        agent = Agent(
            self._build_model(),
            output_type=PageExtractionResult,
            system_prompt=EXTRACTION_SYSTEM_PROMPT,
            retries=1,
        )

        prompt = f"Analyze this page and extract any gated communities mentioned:\n\n{page_text[:self.page_max_chars]}"

        try:
            result = await agent.run(
                prompt,
                model_settings={"max_tokens": self.max_tokens},
            )
            extraction: PageExtractionResult = result.output

            if not extraction.is_relevant:
                logger.debug("Page not relevant: %s", source_url)
                return []

            communities = [
                DiscoveredCommunity.from_llm_item(item, source_url, source_name)
                for item in extraction.communities
                if item.confidence >= 0.5
            ]
            logger.info(
                "Extracted %d communities from %s",
                len(communities),
                source_url,
            )
            return communities

        except Exception as exc:
            logger.warning("LLM extraction failed for %s: %s", source_url, exc)
            return []

    async def extract_communities_from_screenshot(
        self,
        source_url: str,
        source_name: str,
    ) -> list[DiscoveredCommunity]:
        """Use vision capabilities to extract communities from a screenshot."""
        screenshot_path = await take_screenshot(source_url)
        if not screenshot_path:
            logger.warning("Failed to take screenshot of %s", source_url)
            return []

        prompt = """Analyze this webpage screenshot and extract information about gated communities in southern Florida.

For each community you can identify, provide:
- Community name
- City/location if visible
- County if mentioned
- Whether it appears to be gated (look for gates, security mentions, "gated community" text)
- Your confidence level (0.0-1.0)
- Brief evidence from what you see

Focus on residential communities, not commercial properties or individual home listings."""

        try:
            analysis = await analyze_screenshot(
                screenshot_path,
                prompt,
                model_name=self.model_name,
                base_url=self.base_url,
            )
            
            if not analysis:
                logger.warning("Vision analysis returned no results for %s", source_url)
                return []

            # Parse the vision analysis into structured data
            # The LLM should return structured text we can parse
            communities = self._parse_vision_analysis(analysis, source_url, source_name)
            logger.info(
                "Extracted %d communities via vision from %s",
                len(communities),
                source_url,
            )
            return communities

        except Exception as exc:
            logger.warning("Vision extraction failed for %s: %s", source_url, exc)
            return []
        finally:
            # Clean up screenshot file
            try:
                from pathlib import Path as PathLib
                PathLib(screenshot_path).unlink(missing_ok=True)
            except Exception:
                pass

    def _parse_vision_analysis(
        self,
        analysis: str,
        source_url: str,
        source_name: str,
    ) -> list[DiscoveredCommunity]:
        """Parse vision analysis text into DiscoveredCommunity objects."""
        communities = []
        
        # Simple parsing - look for community names in the analysis
        # This is a basic implementation; could be enhanced with more structured parsing
        lines = analysis.split('\n')
        current_community = {}
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Look for community name patterns
            if 'name:' in line.lower() or 'community:' in line.lower():
                if current_community.get('name'):
                    # Save previous community
                    communities.append(self._create_community_from_dict(current_community, source_url, source_name))
                current_community = {'name': line.split(':', 1)[1].strip()}
            elif 'city:' in line.lower() or 'location:' in line.lower():
                current_community['city'] = line.split(':', 1)[1].strip()
            elif 'county:' in line.lower():
                current_community['county'] = line.split(':', 1)[1].strip()
            elif 'confidence:' in line.lower():
                try:
                    conf_str = line.split(':', 1)[1].strip()
                    current_community['confidence'] = float(conf_str)
                except ValueError:
                    pass
            elif 'gated:' in line.lower():
                current_community['is_gated'] = 'yes' in line.lower() or 'true' in line.lower()
            elif 'evidence:' in line.lower():
                current_community['evidence'] = line.split(':', 1)[1].strip()
        
        # Don't forget the last community
        if current_community.get('name'):
            communities.append(self._create_community_from_dict(current_community, source_url, source_name))
        
        return communities

    def _create_community_from_dict(
        self,
        data: dict,
        source_url: str,
        source_name: str,
    ) -> DiscoveredCommunity:
        """Create a DiscoveredCommunity from a dictionary."""
        return DiscoveredCommunity(
            name=data.get('name', 'Unknown'),
            source_url=source_url,
            source_name=source_name,
            city=data.get('city'),
            county=data.get('county'),
            confidence=data.get('confidence', 0.6),
            is_gated=data.get('is_gated', True),
            evidence=data.get('evidence', 'Extracted via vision'),
        )

    async def verify_community(
        self,
        community: DiscoveredCommunity,
    ) -> DiscoveredCommunity:
        """Use the LLM to verify a community candidate."""
        search_query = f'"{community.name}" florida gated community'
        results = web_search(search_query, max_results=5)

        if not results:
            community.verification_notes.append("No search results for verification")
            return community

        evidence_text = []
        for sr in results[:3]:
            page_text = await read_page(sr.url, max_chars=8000)
            if page_text:
                evidence_text.append(f"Source: {sr.url}\n{page_text[:3000]}")

        if not evidence_text:
            community.verification_notes.append("Could not read verification sources")
            return community

        agent = Agent(
            self._build_model(),
            output_type=VerificationResult,
            system_prompt=VERIFICATION_SYSTEM_PROMPT,
            retries=1,
        )

        prompt = f"""Verify this community candidate:

Name: {community.name}
Reported city: {community.city or 'unknown'}
Reported county: {community.county or 'unknown'}

Evidence from web searches:
{''.join(evidence_text)}

Is this a real gated community in southern Florida?"""

        try:
            result = await agent.run(
                prompt,
                model_settings={"max_tokens": self.max_tokens},
            )
            verification: VerificationResult = result.output

            if verification.correct_name:
                community.name = verification.correct_name
            if verification.correct_location:
                community.city = verification.correct_location

            community.verified = (
                verification.is_real_community
                and verification.is_gated
                and verification.is_in_target_area
            )
            community.confidence = 0.9 if community.verified else 0.3
            community.verification_notes.append(verification.reasoning)

            logger.info(
                "Verification for %s: %s",
                community.name,
                "VERIFIED" if community.verified else "REJECTED",
            )

        except Exception as exc:
            logger.warning("LLM verification failed for %s: %s", community.name, exc)
            community.verification_notes.append(f"Verification error: {exc}")

        return community

    def _report_progress(self, message: str) -> None:
        """Report progress via callback."""
        if self.on_progress:
            self.on_progress(message)
        logger.info(message)

    def _report_discovery(self, community: DiscoveredCommunity) -> None:
        """Report a new discovery via callback."""
        if self.on_discovery:
            self.on_discovery(community)

    def _report_verification(self, community: DiscoveredCommunity) -> None:
        """Report a verification result via callback."""
        if self.on_verification:
            self.on_verification(community)

    def _save_state(self) -> None:
        """Save current state to disk."""
        self.state.save(self.state_path)

    def _should_stop(self) -> tuple[bool, Optional[str]]:
        """Check if discovery should stop."""
        if len(self.state.discovered_communities) >= self.max_communities:
            return True, f"Reached max communities ({self.max_communities})"
        
        if self.state.consecutive_empty_pages >= self.dead_end_threshold:
            return True, f"Dead end: {self.dead_end_threshold} consecutive pages with no new communities"
        
        return False, None

    async def discover(self, resume: bool = True) -> DiscoveryResult:
        """Run the full discovery process with resumable state.
        
        Args:
            resume: If True, resume from saved state. If False, start fresh.
        """
        if not resume:
            self.state = DiscoveryState()
            self._save_state()
        
        result = DiscoveryResult()
        
        # Restore previously discovered communities
        for comm_data in self.state.discovered_communities:
            comm = DiscoveredCommunity(
                name=comm_data["name"],
                source_url=comm_data["source_url"],
                source_name=comm_data["source_name"],
                city=comm_data.get("city"),
                county=comm_data.get("county"),
                confidence=comm_data.get("confidence", 0.5),
                is_gated=comm_data.get("is_gated", True),
                evidence=comm_data.get("evidence", ""),
            )
            result.communities.append(comm)
        
        self._report_progress(
            f"Starting discovery (resumed: {len(self.state.discovered_communities)} communities, "
            f"{len(self.state.processed_urls)} URLs processed)"
        )

        for query in self.seed_queries:
            # Skip already processed queries
            if self.state.has_processed_query(query):
                self._report_progress(f"Skipping already processed query: {query}")
                continue
            
            # Check stop conditions
            should_stop, reason = self._should_stop()
            if should_stop:
                self.state.stopped_reason = reason
                self._save_state()
                self._report_progress(f"Stopping: {reason}")
                break
            
            result.queries_used.append(query)
            self._report_progress(f"Searching: {query}")

            search_results = web_search(query, max_results=self.max_results_per_query)
            if not search_results:
                self._report_progress(f"No results for query: {query}")
                self.state.processed_queries.append(query)
                self._save_state()
                continue

            query_found_new = False
            
            for sr in search_results:
                # Check stop conditions
                should_stop, reason = self._should_stop()
                if should_stop:
                    self.state.stopped_reason = reason
                    self._save_state()
                    self._report_progress(f"Stopping: {reason}")
                    break
                
                # Skip already processed URLs
                if self.state.has_processed_url(sr.url):
                    self._report_progress(f"Skipping already processed URL: {sr.url}")
                    continue
                
                result.sources_consulted.append(sr.url)
                self._report_progress(f"Reading: {sr.url}")

                # Try text extraction first
                page_text = await read_page(sr.url, max_chars=self.page_max_chars)
                
                # Use text extraction if we got good content, otherwise fall back to vision
                if page_text and len(page_text.strip()) >= 100:
                    communities = await self.extract_communities_from_page(
                        page_text, sr.url, sr.title or sr.url, use_vision=False
                    )
                else:
                    # Text extraction failed or returned insufficient content, use vision
                    self._report_progress(f"Text extraction insufficient for {sr.url}, using vision")
                    communities = await self.extract_communities_from_screenshot(
                        sr.url, sr.title or sr.url
                    )

                # Process discovered communities
                new_found_this_page = 0
                for comm in communities:
                    # Skip duplicates
                    if self.state.has_discovered(comm.name):
                        continue
                    
                    # Add to state and result
                    self.state.discovered_communities.append(comm.to_dict())
                    result.communities.append(comm)
                    new_found_this_page += 1
                    
                    # Report discovery
                    self._report_discovery(comm)
                    self._report_progress(
                        f"Discovered #{len(self.state.discovered_communities)}: {comm.name} "
                        f"({comm.city or 'unknown location'})"
                    )
                    
                    # Save state after each discovery
                    self._save_state()
                
                # Track consecutive empty pages for dead end detection
                if new_found_this_page == 0:
                    self.state.consecutive_empty_pages += 1
                    self._report_progress(
                        f"No new communities from {sr.url} "
                        f"(consecutive empty: {self.state.consecutive_empty_pages})"
                    )
                else:
                    self.state.consecutive_empty_pages = 0
                    query_found_new = True
                
                # Mark URL as processed
                self.state.processed_urls.append(sr.url)
                self._save_state()
            
            # Mark query as processed
            self.state.processed_queries.append(query)
            self._save_state()
            
            # Check stop conditions again
            should_stop, reason = self._should_stop()
            if should_stop:
                self.state.stopped_reason = reason
                self._save_state()
                self._report_progress(f"Stopping: {reason}")
                break

        self._report_progress(
            f"Discovery complete: {len(self.state.discovered_communities)} communities found"
        )
        return result

    async def verify_all(
        self,
        communities: list[DiscoveredCommunity],
    ) -> list[DiscoveredCommunity]:
        """Verify a list of communities using the LLM."""
        verified = []
        for comm in communities:
            # Skip already verified
            if any(v["name"] == comm.name for v in self.state.verified_communities):
                self._report_progress(f"Skipping already verified: {comm.name}")
                comm.verified = True
                verified.append(comm)
                continue
            
            result = await self.verify_community(comm)
            verified.append(result)
            
            # Track verified communities
            if result.verified:
                self.state.verified_communities.append(result.to_dict())
            
            # Report verification
            self._report_verification(result)
            status = "VERIFIED" if result.verified else "REJECTED"
            self._report_progress(
                f"Verification #{len(verified)}/{len(communities)}: {result.name} - {status}"
            )
            
            # Save state after each verification
            self._save_state()
            await asyncio.sleep(1)
        return verified

    def save_target_list(
        self,
        communities: list[DiscoveredCommunity],
        path: Path,
    ) -> None:
        """Save verified communities as target_list.json."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "communities": [
                {
                    "name": c.name,
                    "city": c.city,
                    "county_fips": None,
                    "notes": "; ".join(c.verification_notes) if c.verification_notes else None,
                }
                for c in communities
                if c.verified
            ]
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("Saved %d verified communities to %s", len(data["communities"]), path)


async def main() -> None:
    """CLI entry point for community discovery."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Discover gated communities in southern Florida using the local LLM"
    )
    parser.add_argument(
        "--output",
        type=str,
        default="data/communities/target_list.json",
        help="Output path for target_list.json",
    )
    parser.add_argument(
        "--state-path",
        type=str,
        default="data/communities/discovery_state.json",
        help="Path to save/load discovery state for resumability",
    )
    parser.add_argument(
        "--max-communities",
        type=int,
        default=200,
        help="Maximum communities to discover before stopping (default: 200)",
    )
    parser.add_argument(
        "--dead-end-threshold",
        type=int,
        default=10,
        help="Stop after N consecutive pages with no new communities (default: 10)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify discovered communities with the LLM (slower but more accurate)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset discovery state and start fresh",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Progress callback for console output
    def on_progress(message: str) -> None:
        print(f"[PROGRESS] {message}")

    def on_discovery(community: DiscoveredCommunity) -> None:
        print(f"[DISCOVERED] {community.name} ({community.city or 'unknown'})")

    def on_verification(community: DiscoveredCommunity) -> None:
        status = "VERIFIED" if community.verified else "REJECTED"
        print(f"[{status}] {community.name}")

    engine = CommunityDiscoveryEngine(
        state_path=Path(args.state_path),
        max_communities=args.max_communities,
        dead_end_threshold=args.dead_end_threshold,
        on_progress=on_progress,
        on_discovery=on_discovery,
        on_verification=on_verification,
    )

    # Run discovery (resume by default, unless --reset)
    result = await engine.discover(resume=not args.reset)

    if args.verify:
        print(f"\n[PHASE 2] Verifying {len(result.communities)} communities...")
        result.communities = await engine.verify_all(result.communities)
        verified = [c for c in result.communities if c.verified]
        engine.save_target_list(verified, Path(args.output))
        print(f"\n[DONE] Verified {len(verified)} communities, saved to {args.output}")
    else:
        print(f"\n[DONE] Discovered {len(result.communities)} communities (not verified)")
        output_path = Path(args.output).with_suffix(".discovered.json")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result.to_dict(), indent=2), encoding="utf-8"
        )
        print(f"[SAVED] Results to {output_path}")

    # Print summary
    if engine.state.stopped_reason:
        print(f"\n[STOPPED] Reason: {engine.state.stopped_reason}")
        print(f"[RESUME] Run again without --reset to continue from where it stopped")


if __name__ == "__main__":
    asyncio.run(main())
