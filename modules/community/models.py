"""Extraction and storage models for Workstream B community research (FSD §3b).

The research agent proposes facts as ``CommunityFacts``; code validates them
before anything is written to the store. Every fact carries provenance
(``source_url``, ``retrieved_at``, ``confidence``) so conflicting sources
coexist instead of silently overwriting each other.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

FeeType = Literal["hoa_monthly", "hoa_annual", "cdd_assessment", "other"]
ProximityCategory = Literal["shopping", "grocery", "library", "hospital", "school", "other"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


FLORIDA_COUNTIES = [
    "Alachua", "Baker", "Bay", "Bradford", "Brevard", "Broward", "Calhoun",
    "Charlotte", "Citrus", "Clay", "Collier", "Columbia", "DeSoto", "Dixie",
    "Duval", "Escambia", "Flagler", "Franklin", "Gadsden", "Gilchrist",
    "Glades", "Gulf", "Hamilton", "Hardee", "Hendry", "Hernando", "Highlands",
    "Hillsborough", "Holmes", "Indian River", "Jackson", "Jefferson", "Lafayette",
    "Lake", "Lee", "Leon", "Levy", "Liberty", "Madison", "Manatee", "Marion",
    "Martin", "Miami-Dade", "Monroe", "Nassau", "Okaloosa", "Okeechobee", "Orange",
    "Osceola", "Palm Beach", "Pasco", "Pinellas", "Polk", "Putnam",
    "Santa Rosa", "Sarasota", "Seminole", "St. Johns", "St. Lucie",
    "Sumter", "Suwannee", "Taylor", "Union", "Volusia", "Wakulla", "Walton", "Washington",
]


class SourceFact(BaseModel):
    """Base for every fact recorded by the research assistant."""

    source_url: str = Field(min_length=1)
    retrieved_at: datetime = Field(default_factory=utcnow)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("source_url")
    @classmethod
    def _must_be_http_or_ai_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://", "ai:", "browser:")):
            raise ValueError("source_url must be an http(s) URL, ai:, or browser: scheme")
        return v


class FeeFact(SourceFact):
    fee_type: FeeType
    amount: Optional[float] = Field(default=None, ge=0)
    period: Optional[str] = None
    currency: str = "USD"
    note: Optional[str] = None


class AmenityFact(SourceFact):
    amenity: str
    detail: Optional[str] = None

    @field_validator("amenity")
    @classmethod
    def _normalize_key(cls, v: str) -> str:
        key = re.sub(r"[^a-z0-9]+", "_", v.strip().lower()).strip("_")
        if not key:
            raise ValueError("amenity must not be empty")
        return key


class Demographics(SourceFact):
    median_age: Optional[float] = None
    median_household_income: Optional[float] = Field(default=None, ge=0)
    owner_occupancy_pct: Optional[float] = Field(default=None, ge=0, le=100)
    population: Optional[int] = Field(default=None, ge=0)
    data_year: Optional[int] = None
    geography_level: Optional[str] = None


class ProximityMetric(SourceFact):
    category: ProximityCategory
    nearest_name: Optional[str] = None
    distance_miles: Optional[float] = Field(default=None, ge=0)


class CommunityFacts(BaseModel):
    """Structured output produced by the research agent for one community."""

    fees: list[FeeFact] = Field(default_factory=list)
    amenities: list[AmenityFact] = Field(default_factory=list)
    demographics: Optional[Demographics] = None
    proximity: list[ProximityMetric] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class CommunityIdentity(BaseModel):
    name: str
    slug: str
    county_fips: Optional[str] = None
    city: Optional[str] = None
    hoa_name: Optional[str] = None
    cdd_name: Optional[str] = None
    is_gated: Optional[bool] = None
    geo_point: Optional[dict] = None
    notes: Optional[str] = None


class Discrepancy(BaseModel):
    field: str
    values: list[dict]
    detected_at: datetime = Field(default_factory=utcnow)


class CommunityRecord(BaseModel):
    """Stored document for one community (file-based MVP; PostGIS in M5)."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    identity: CommunityIdentity
    fees: list[FeeFact] = Field(default_factory=list)
    amenities: list[AmenityFact] = Field(default_factory=list)
    demographics: Optional[Demographics] = None
    demographics_history: list[Demographics] = Field(default_factory=list)
    proximity: list[ProximityMetric] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ResearchLogEntry(BaseModel):
    """Append-only audit trail entry (one per agent tool call / extraction)."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    community_slug: Optional[str] = None
    action: Literal["search", "read_page", "extract", "screenshot"]
    query: Optional[str] = None
    url: Optional[str] = None
    summary: Optional[str] = None
    model_name: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class DiscoveredCommunityItem(BaseModel):
    """A community extracted by the LLM from a page."""

    name: str = Field(min_length=2, description="Full name of the community")
    city: Optional[str] = Field(default=None, description="City or area where located")
    county: Optional[str] = Field(default=None, description="County name")
    is_gated: bool = Field(default=True, description="Whether this is a gated community")
    confidence: float = Field(default=0.7, ge=0.0, le=1.0, description="Confidence this is a real gated community")
    evidence: str = Field(default="", description="Brief quote or evidence from the page supporting this finding")


class PageExtractionResult(BaseModel):
    """LLM output for extracting communities from a single page."""

    communities: list[DiscoveredCommunityItem] = Field(default_factory=list)
    page_topic: str = Field(default="", description="What this page is about (directory, listing, article, etc.)")
    is_relevant: bool = Field(default=True, description="Whether the page contains community listings")
    notes: list[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    """LLM output for verifying a community candidate."""

    is_real_community: bool = Field(description="Whether this appears to be a real community")
    is_gated: bool = Field(default=False, description="Whether it is confirmed as gated")
    is_in_target_area: bool = Field(default=False, description="Whether it is in southern Florida")
    correct_name: Optional[str] = Field(default=None, description="Corrected name if the candidate was wrong")
    correct_location: Optional[str] = Field(default=None, description="Correct city/area if different")
    reasoning: str = Field(default="", description="Brief explanation of the verification decision")


AI_SOURCE_URL = "ai:condensed"


class CondensedCommunityItem(BaseModel):
    """AI-condensed output for one community — used by the bulk condenser."""

    name: str = Field(min_length=2, description="Full name of the community")
    city: Optional[str] = Field(default=None, description="City or area where located")
    county: Optional[str] = Field(default=None, description="County name")
    is_gated: bool = Field(default=True, description="Whether this is a gated community")
    confidence: float = Field(default=0.6, ge=0.0, le=1.0, description="Confidence in this data")
    overview: Optional[str] = Field(default=None, description="Brief description/overview of the community")
    fees: list[FeeFact] = Field(default_factory=list, description="HOA fees, CDD assessments, etc.")
    amenities: list[AmenityFact] = Field(default_factory=list, description="Community amenities")
    demographics: Optional[Demographics] = Field(default=None, description="Demographic profile")
    proximity: list[ProximityMetric] = Field(default_factory=list, description="Nearby services/facilities")
    hoa_name: Optional[str] = Field(default=None, description="HOA association name if known")

    def to_record(self) -> CommunityRecord:
        from .store import slugify

        slug = slugify(self.name)
        identity = CommunityIdentity(
            name=self.name,
            slug=slug,
            city=self.city,
            hoa_name=self.hoa_name,
            is_gated=self.is_gated,
            notes=self.overview,
        )
        facts = CommunityFacts(
            fees=self.fees,
            amenities=self.amenities,
            demographics=self.demographics,
            proximity=self.proximity,
        )
        record = CommunityRecord(identity=identity)
        from .store import merge_facts

        merge_facts(record, facts)
        return record


class CondensedCommunityBatch(BaseModel):
    """AI output for a batch of condensed communities."""

    communities: list[CondensedCommunityItem] = Field(default_factory=list)
    query_description: str = Field(default="", description="What was queried")
    notes: list[str] = Field(default_factory=list, description="Any caveats or notes from the AI")


BROWSER_SOURCE_URL = "browser:google"


class GoogleSearchResult(BaseModel):
    """A single result from Google Search via browser automation."""

    title: str = Field(min_length=1, description="Result title")
    url: str = Field(min_length=1, description="Result URL")
    snippet: str = Field(default="", description="Result snippet/description")
    position: int = Field(default=0, ge=0, description="Position in search results (1-indexed)")


class GapAnalysis(BaseModel):
    """AI output for analyzing gaps in collected data.

    The local model reviews what data we already have and identifies
    what's missing. Prioritizes data completeness over geographic coverage,
    but also tracks county-level gaps for full Florida coverage.
    """

    total_communities: int = Field(default=0, description="Total communities in database")
    communities_missing_fees: list[str] = Field(
        default_factory=list, description="Community names missing fee data"
    )
    communities_missing_amenities: list[str] = Field(
        default_factory=list, description="Community names missing amenity data"
    )
    communities_missing_demographics: list[str] = Field(
        default_factory=list, description="Community names missing demographic data"
    )
    communities_missing_proximity: list[str] = Field(
        default_factory=list, description="Community names missing proximity data"
    )
    counties_with_data: list[str] = Field(
        default_factory=list, description="Counties that have at least one community with data"
    )
    counties_missing_data: list[str] = Field(
        default_factory=list, description="Counties with few or no communities"
    )
    priority_gap: Literal["fees", "amenities", "demographics", "proximity", "geographic"] = Field(
        default="fees", description="The most critical gap to fill next"
    )
    priority_target: str = Field(
        default="", description="Specific community or county to target next"
    )
    reasoning: str = Field(
        default="", description="Brief explanation of why this gap is priority"
    )
    has_any_gaps: bool = Field(
        default=True, description="Whether there are any gaps remaining"
    )


class NextQuery(BaseModel):
    """AI output for generating the next search query."""

    query: str = Field(min_length=3, description="The Google search query to execute")
    reasoning: str = Field(
        default="", description="Why this query was chosen based on gap analysis"
    )
    target_type: Literal["discover", "enrich", "geographic"] = Field(
        default="discover",
        description="Type: discover (find new communities), enrich (fill data for known community), geographic (cover a county)"
    )
    expected_result: str = Field(
        default="", description="What we hope to find from this query"
    )


class BrowserCondenserState(BaseModel):
    """Persistent state for the browser condenser accumulation loop.

    Saved to data/communities/browser_condenser_state.json for resumability.
    Tracks all queries executed, communities found, and current progress.
    """

    queries_executed: list[str] = Field(
        default_factory=list, description="All queries that have been executed"
    )
    communities_discovered: list[str] = Field(
        default_factory=list, description="Names of communities discovered so far"
    )
    communities_enriched: list[str] = Field(
        default_factory=list, description="Names of communities that have been enriched"
    )
    total_iterations: int = Field(default=0, description="Number of iterations completed")
    last_gap_analysis: Optional[GapAnalysis] = Field(
        default=None, description="Most recent gap analysis result"
    )
    last_query: Optional[NextQuery] = Field(
        default=None, description="Most recent query executed"
    )
    stopped_reason: Optional[str] = Field(
        default=None, description="Why the accumulation stopped (if applicable)"
    )
    data_sources_count: dict[str, int] = Field(
        default_factory=dict, description="Count of communities per data source"
    )


# ── Vision Browser Agent Models ─────────────────────────────────────────────


class BrowserAction(BaseModel):
    """Action the LLM wants to perform on the visible browser page.
    
    The vision-driven browser agent sends screenshots to the LLM, which
    returns this structured action. The agent executes it via Playwright,
    then loops until the LLM signals "done" or "give_up".
    """

    action: Literal[
        "click",           # Click at coordinates (x, y)
        "captcha_click",   # Click CAPTCHA checkbox at coordinates
        "type",            # Type text into a field (selector or coordinates)
        "scroll",          # Scroll the page (direction: up/down)
        "wait",            # Wait N seconds for page to load
        "done",            # Page content extracted successfully
        "give_up",         # Cannot proceed (error, blocked, etc.)
    ] = Field(
        description="What action to perform on the page"
    )
    
    x: Optional[int] = Field(
        default=None,
        description="Pixel X coordinate for click/captcha_click (relative to screenshot top-left)"
    )
    y: Optional[int] = Field(
        default=None,
        description="Pixel Y coordinate for click/captcha_click"
    )
    
    selector: Optional[str] = Field(
        default=None,
        description="CSS selector for type action (alternative to coordinates)"
    )
    text: Optional[str] = Field(
        default=None,
        description="Text to type for 'type' action"
    )
    
    direction: Optional[Literal["up", "down"]] = Field(
        default=None,
        description="Scroll direction for 'scroll' action"
    )
    
    seconds: Optional[float] = Field(
        default=None,
        description="Seconds to wait for 'wait' action"
    )
    
    summary: Optional[str] = Field(
        default=None,
        description="Brief summary of page content (for 'done' action)"
    )
    
    page_content: Optional[str] = Field(
        default=None,
        description="Full extracted text content from the page (for 'done' action)"
    )
    
    error_description: Optional[str] = Field(
        default=None,
        description="Description of error or blocker (for 'give_up' action)"
    )
    
    reasoning: str = Field(
        default="",
        description="Why the LLM chose this action"
    )


class BrowserExtractResult(BaseModel):
    """Result from the vision browser agent attempting to access and extract content from a URL."""

    url: str = Field(description="The URL that was accessed")
    success: bool = Field(description="Whether content was successfully extracted")
    status: Literal["printed", "failed_4xx", "failed_5xx", "failed_timeout", "failed_network", "blocked"] = Field(
        description="Final status of the access attempt"
    )
    
    # Content
    page_title: Optional[str] = Field(default=None, description="Extracted page <title>")
    page_content: Optional[str] = Field(default=None, description="Extracted text content")
    content_length: int = Field(default=0, description="Length of extracted text in characters")
    
    # Artifacts
    pdf_path: Optional[str] = Field(default=None, description="Path to saved PDF")
    screenshot_path: Optional[str] = Field(default=None, description="Path to last screenshot")
    
    # Failure tracking
    http_status: Optional[int] = Field(default=None, description="HTTP status code if available")
    failure_category: Optional[Literal[
        "cloudflare_block",
        "bot_detection",
        "auth_required",
        "not_found",
        "rate_limited",
        "forbidden",
        "bad_request",
        "geo_blocked",
        "timeout",
        "network_error",
        "unknown",
    ]] = Field(default=None, description="Classification of failure reason")
    failure_detail: Optional[str] = Field(default=None, description="Error response snippet or description")
    is_retryable: bool = Field(default=False, description="Whether this failure may resolve itself on retry")
    
    # CAPTCHA tracking
    captcha_detected: bool = Field(default=False, description="Whether a CAPTCHA was encountered")
    captcha_solved: bool = Field(default=False, description="Whether the CAPTCHA was successfully solved")
    
    # Execution metadata
    steps_taken: int = Field(default=0, description="Number of LLM action steps in the vision loop")
    elapsed_seconds: float = Field(default=0.0, description="Total time spent on this URL")
    
    # Data quality metrics
    has_community_data: bool = Field(default=False, description="Whether the page contains relevant community data")
    data_quality_score: float = Field(default=0.0, description="Quality score 0-100, larger is better")
    data_types_found: Optional[str] = Field(default=None, description="Comma-separated list of data types found (fees,amenities,demographics,proximity)")
    data_summary: Optional[str] = Field(default=None, description="Brief summary of extracted data")

