"""Full community data pipeline — discover and condense using all available methods.

Runs every data collection method against every known community, one community
at a time.  Updates the database after each condenser run and persists pipeline
state so the process can be stopped and restarted at any point.

Condenser order per community (cheapest/fastest first):
  1. ai_condenser      — local model knowledge (no web, fast)
  2. web_condenser     — DuckDuckGo search + local model
  3. research_agent    — deep research with web_search / read_page tools
  4. browser_condenser — Google search via Chrome + local model extraction
  5. gemini_condenser  — Gemini API with web search grounding (optional)

State file layout (``data/pipeline_state.json``)::

    {
      "version": 1,
      "started_at": "...",
      "last_updated": "...",
      "condensers": ["ai", "web", "research", "browser", "gemini"],
      "communities": [
        {"name": "...", "slug": "...", "city": "...", "source": "target_list"},
        ...
      ],
      "results": {
        "<slug>": {
          "ai":      {"status": "done",  "elapsed": 3.2, "fees": 2, "amenities": 8, ...},
          "web":     {"status": "pending"},
          "research":{"status": "pending"},
          "browser": {"status": "pending"},
          "gemini":  {"status": "pending"}
        },
        ...
      },
      "stats": {"total_runs": 0, "successful": 0, "failed": 0, "skipped": 0}
    }

CLI::

    python -m modules.community.full_pipeline
    python -m modules.community.full_pipeline --reset
    python -m modules.community.full_pipeline --community "Pelican Bay"
    python -m modules.community.full_pipeline --status
    python -m modules.community.full_pipeline --condensers ai,web
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from .agent import CommunityResearchAgent
from .database import CommunityDatabase
from .models import (
    AI_SOURCE_URL,
    BROWSER_SOURCE_URL,
    CommunityFacts,
    CommunityIdentity,
    CommunityRecord,
    CondensedCommunityItem,
    Demographics,
    FeeFact,
    AmenityFact,
    ProximityMetric,
    utcnow,
)
from .store import CommunityStore, merge_facts, slugify
from .url_tracker import URLTracker

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

ALL_CONDENSERS = ["ai", "web", "research", "browser", "gemini"]

CONDENSERS_WITHOUT_OPTIONAL = ["ai", "web", "research", "browser"]

DEFAULT_STATE_PATH = Path("data") / "pipeline_state.json"

# ── State models ─────────────────────────────────────────────────────────────


class CommunityInfo(BaseModel):
    """A community to be processed by the pipeline."""

    name: str
    slug: str
    city: Optional[str] = None
    county: Optional[str] = None
    source: str = "unknown"  # target_list, discovery, database


class CondenserStepResult(BaseModel):
    """Result of running one condenser on one community."""

    status: str = "pending"  # pending | running | done | error | skipped
    elapsed: float = 0.0
    fees: int = 0
    amenities: int = 0
    proximity: int = 0
    demographics_present: bool = False
    sources_consulted: int = 0
    errors: list[str] = Field(default_factory=list)


class PipelineStats(BaseModel):
    """Aggregate pipeline statistics."""

    total_runs: int = 0
    successful: int = 0
    failed: int = 0
    skipped: int = 0
    started_at: Optional[str] = None
    last_updated: Optional[str] = None


class PipelineState(BaseModel):
    """Complete pipeline state — serialized to JSON for resumability."""

    version: int = 1
    started_at: Optional[str] = None
    last_updated: Optional[str] = None
    condensers: list[str] = Field(default_factory=lambda: list(ALL_CONDENSERS))
    communities: list[CommunityInfo] = Field(default_factory=list)
    results: dict[str, dict[str, CondenserStepResult]] = Field(default_factory=dict)
    stats: PipelineStats = Field(default_factory=PipelineStats)

    # ── serialisation helpers ────────────────────────────────────────────

    def save(self, path: Path) -> None:
        self.last_updated = _now_iso()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> PipelineState:
        if path.exists():
            try:
                return cls.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Failed to load pipeline state from %s: %s", path, exc)
        return cls()

    # ── query helpers ────────────────────────────────────────────────────

    def community_slugs(self) -> list[str]:
        return [c.slug for c in self.communities]

    def pending_work(self) -> list[tuple[str, str]]:
        """Return list of (slug, condenser) pairs still to run, in order."""
        work: list[tuple[str, str]] = []
        for ci in self.communities:
            for cond in self.condensers:
                step = self.results.get(ci.slug, {}).get(cond)
                if step is None or step.status in ("pending", "error"):
                    work.append((ci.slug, cond))
        return work

    def community_progress(self, slug: str) -> dict[str, str]:
        """Return {condenser: status} for one community."""
        return {
            cond: (self.results.get(slug, {}).get(cond, CondenserStepResult())).status
            for cond in self.condensers
        }


# ── Discovery of known communities ───────────────────────────────────────────


def _discover_all_communities(
    store: CommunityStore,
    database: CommunityDatabase,
    discovery_state_path: Path | None = None,
) -> list[CommunityInfo]:
    """Gather communities from every known source, deduplicated by slug."""
    seen: dict[str, CommunityInfo] = {}

    # Source 1: target_list.json
    for seed in store.load_target_list():
        slug = slugify(seed["name"])
        if slug not in seen:
            seen[slug] = CommunityInfo(
                name=seed["name"],
                slug=slug,
                city=seed.get("city"),
                county=seed.get("county_fips"),
                source="target_list",
            )

    # Source 2: discovery_state.json
    dsp = discovery_state_path or Path("data/communities/discovery_state.json")
    if dsp.exists():
        try:
            ds = json.loads(dsp.read_text(encoding="utf-8"))
            for dc in ds.get("discovered_communities", []):
                name = dc.get("name", "")
                slug = slugify(name)
                if slug and slug not in seen:
                    seen[slug] = CommunityInfo(
                        name=name,
                        slug=slug,
                        city=dc.get("city"),
                        county=dc.get("county"),
                        source="discovery",
                    )
        except Exception as exc:
            logger.warning("Could not read discovery state: %s", exc)

    # Source 3: database
    try:
        for record in database.list_records():
            slug = record.identity.slug
            if slug and slug not in seen:
                seen[slug] = CommunityInfo(
                    name=record.identity.name,
                    slug=slug,
                    city=record.identity.city,
                    county=record.identity.county_fips,
                    source="database",
                )
    except Exception as exc:
        logger.warning("Could not list database records: %s", exc)

    return list(seen.values())


# ── Helpers ──────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_summary(record: CommunityRecord) -> dict[str, Any]:
    return {
        "fees": len(record.fees),
        "amenities": len(record.amenities),
        "proximity": len(record.proximity),
        "demographics_present": record.demographics is not None,
    }


def _save_condensed_item(
    item: CondensedCommunityItem,
    store: CommunityStore,
    database: CommunityDatabase,
    data_source: str,
) -> str:
    """Convert condensed item → record → save to store + database."""
    record = item.to_record()
    store.save_community(record)
    database.upsert_record(record, data_source=data_source)
    return record.identity.slug


def _ensure_community_record(
    info: CommunityInfo,
    store: CommunityStore,
    database: CommunityDatabase,
) -> CommunityRecord:
    """Make sure a CommunityRecord exists for this community (create if needed)."""
    existing = store.load_community(info.slug)
    if existing is not None:
        return existing
    seed = {
        "name": info.name,
        "slug": info.slug,
        "city": info.city,
        "county_fips": info.county,
        "is_gated": True,
    }
    record = store.upsert_identity(seed)
    store.save_community(record)
    database.upsert_record(record, data_source="pipeline")
    return record


# ── Per-condenser runners ────────────────────────────────────────────────────


async def _run_ai_condenser(
    info: CommunityInfo,
    store: CommunityStore,
    database: CommunityDatabase,
    url_tracker: URLTracker,
    on_progress: Optional[Callable[[str], None]] = None,
) -> CondenserStepResult:
    """Run the AI condenser for one community."""
    from .ai_condenser import AICondenser

    condenser = AICondenser(store=store, database=database)
    started = time.monotonic()
    result = CondenserStepResult(status="running")
    try:
        item = await condenser.enrich_community(info.name, city=info.city)
        # Normalize source URLs
        for fee in item.fees:
            if not fee.source_url.startswith(("http://", "https://", "ai:")):
                fee.source_url = AI_SOURCE_URL
        for amenity in item.amenities:
            if not amenity.source_url.startswith(("http://", "https://", "ai:")):
                amenity.source_url = AI_SOURCE_URL
        if item.demographics and not item.demographics.source_url.startswith(
            ("http://", "https://", "ai:")
        ):
            item.demographics.source_url = AI_SOURCE_URL
        for prox in item.proximity:
            if not prox.source_url.startswith(("http://", "https://", "ai:")):
                prox.source_url = AI_SOURCE_URL
        _save_condensed_item(item, store, database, "ai_condenser")
        result.status = "done"
        result.fees = len(item.fees)
        result.amenities = len(item.amenities)
        result.proximity = len(item.proximity)
        result.demographics_present = item.demographics is not None
    except Exception as exc:
        result.status = "error"
        result.errors.append(str(exc))
        logger.error("AI condenser failed for %s: %s", info.name, exc)
    result.elapsed = time.monotonic() - started
    return result


async def _run_web_condenser(
    info: CommunityInfo,
    store: CommunityStore,
    database: CommunityDatabase,
    url_tracker: URLTracker,
    on_progress: Optional[Callable[[str], None]] = None,
) -> CondenserStepResult:
    """Run the web condenser for one community."""
    from .web_condenser import WebAICondenser

    condenser = WebAICondenser(store=store, database=database, url_tracker=url_tracker)
    started = time.monotonic()
    result = CondenserStepResult(status="running")
    try:
        item = await condenser.enrich_from_web(info.name, city=info.city)
        # Normalize source URLs
        for fee in item.fees:
            if not fee.source_url.startswith(("http://", "https://", "web:")):
                fee.source_url = "web:extracted"
        for amenity in item.amenities:
            if not amenity.source_url.startswith(("http://", "https://", "web:")):
                amenity.source_url = "web:extracted"
        if item.demographics and not item.demographics.source_url.startswith(
            ("http://", "https://", "web:")
        ):
            item.demographics.source_url = "web:extracted"
        for prox in item.proximity:
            if not prox.source_url.startswith(("http://", "https://", "web:")):
                prox.source_url = "web:extracted"
        _save_condensed_item(item, store, database, "web_condenser")
        result.status = "done"
        result.fees = len(item.fees)
        result.amenities = len(item.amenities)
        result.proximity = len(item.proximity)
        result.demographics_present = item.demographics is not None
    except Exception as exc:
        result.status = "error"
        result.errors.append(str(exc))
        logger.error("Web condenser failed for %s: %s", info.name, exc)
    result.elapsed = time.monotonic() - started
    return result


async def _run_research_agent(
    info: CommunityInfo,
    store: CommunityStore,
    database: CommunityDatabase,
    url_tracker: URLTracker,
    on_progress: Optional[Callable[[str], None]] = None,
) -> CondenserStepResult:
    """Run the research agent for one community."""
    from .research_engine import CommunityResearchEngine

    engine = CommunityResearchEngine(store=store)
    started = time.monotonic()
    result = CondenserStepResult(status="running")
    try:
        seed = {
            "name": info.name,
            "slug": info.slug,
            "city": info.city,
            "county_fips": info.county,
            "is_gated": True,
        }
        report = await engine.research_one(seed)
        # Import the file-based record into the SQLite database
        database.import_from_store()
        result.status = "done" if not report.errors else "done"
        result.fees = report.fees
        result.amenities = report.amenities
        result.proximity = report.proximity
        result.demographics_present = report.demographics_present
        result.sources_consulted = report.tool_calls
        if report.errors:
            result.errors.extend(report.errors)
    except Exception as exc:
        result.status = "error"
        result.errors.append(str(exc))
        logger.error("Research agent failed for %s: %s", info.name, exc)
    result.elapsed = time.monotonic() - started
    return result


async def _run_browser_condenser(
    info: CommunityInfo,
    store: CommunityStore,
    database: CommunityDatabase,
    url_tracker: URLTracker,
    on_progress: Optional[Callable[[str], None]] = None,
) -> CondenserStepResult:
    """Run a targeted browser search for one community.

    Unlike the full browser condenser loop (which analyzes gaps globally),
    this does a focused Google search for one specific community.
    """
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from .tools import google_search, read_page

    started = time.monotonic()
    result = CondenserStepResult(status="running")

    model = OpenAIChatModel(
        "qwen3.8-27b",
        provider=OpenAIProvider(
            base_url="http://localhost:8080/v1",
            api_key="not-needed",
        ),
    )

    try:
        # Build targeted queries for this community
        city_hint = f" {info.city}" if info.city else ""
        queries = [
            f'"{info.name}"{city_hint} Florida HOA fees',
            f'"{info.name}"{city_hint} amenities',
            f'"{info.name}"{city_hint} demographics population',
        ]

        all_items: list[CondensedCommunityItem] = []

        for query in queries:
            if on_progress:
                on_progress(f"  Browser search: {query}")

            try:
                search_results = await google_search(
                    query=query,
                    max_results=5,
                    headless=True,
                    delay_seconds=1.0,
                )
            except Exception as exc:
                logger.warning("Browser search failed for query %s: %s", query, exc)
                continue

            if not search_results:
                continue

            # Register URLs immediately when discovered
            urls = [sr.url for sr in search_results]
            url_tracker.register_urls(urls, discovered_by="browser_condenser")

            # Read top 2 results
            for sr in search_results[:2]:
                try:
                    page_text = await read_page(sr.url, max_chars=6000, use_js=True)
                except Exception:
                    # Record failure in URL tracker
                    url_tracker.update_url_status(
                        sr.url,
                        status="failed",
                        error_message="Page could not be read",
                        http_status=400
                    )
                    continue
                
                if not page_text:
                    # Record failure in URL tracker
                    url_tracker.update_url_status(
                        sr.url,
                        status="failed",
                        error_message="Empty page content",
                        http_status=400
                    )
                    continue

                # Update URL with quality score after successful read
                from .url_tracker import calculate_data_quality
                quality_metrics = calculate_data_quality(page_text)
                url_tracker.update_url_quality(
                    sr.url,
                    has_community_data=quality_metrics["has_community_data"],
                    data_quality_score=quality_metrics["data_quality_score"],
                    data_types_found=quality_metrics["data_types_found"],
                    data_summary=quality_metrics["data_summary"],
                    status="printed",
                )

                extraction_prompt = (
                    f"Extract information about '{info.name}' from this page.\n\n"
                    f"Page: {sr.title}\nURL: {sr.url}\n\n"
                    f"{page_text[:4000]}\n\n"
                    f"Extract fees, amenities, demographics, and proximity data "
                    f"for this community. Use '{BROWSER_SOURCE_URL}' as source_url."
                )

                agent = Agent(
                    model,
                    output_type=CondensedCommunityItem,
                    system_prompt=(
                        "You extract real estate data from web pages. "
                        "Only extract what is explicitly stated. "
                        f"Use {BROWSER_SOURCE_URL} for all source_urls."
                    ),
                    retries=1,
                )

                try:
                    extraction = await agent.run(
                        extraction_prompt,
                        model_settings={"max_tokens": 4096},
                    )
                    item = extraction.output
                    # Set source URLs
                    for fee in item.fees:
                        if not fee.source_url.startswith(("http://", "https://", "browser:")):
                            fee.source_url = sr.url
                    for amenity in item.amenities:
                        if not amenity.source_url.startswith(("http://", "https://", "browser:")):
                            amenity.source_url = sr.url
                    if item.demographics and not item.demographics.source_url.startswith(
                        ("http://", "https://", "browser:")
                    ):
                        item.demographics.source_url = sr.url
                    for prox in item.proximity:
                        if not prox.source_url.startswith(("http://", "https://", "browser:")):
                            prox.source_url = sr.url
                    all_items.append(item)
                except Exception as exc:
                    logger.warning("Extraction failed from %s: %s", sr.url, exc)

        # Merge all extracted items into the record
        if all_items:
            record = store.load_community(info.slug) or _ensure_community_record(
                info, store, database
            )
            for item in all_items:
                facts = CommunityFacts(
                    fees=item.fees,
                    amenities=item.amenities,
                    demographics=item.demographics,
                    proximity=item.proximity,
                )
                merge_facts(record, facts)
            store.save_community(record)
            database.upsert_record(record, data_source="browser_condenser")

            result.status = "done"
            result.fees = len(all_items[0].fees)
            result.amenities = len(all_items[0].amenities)
            result.proximity = len(all_items[0].proximity)
            result.demographics_present = all_items[0].demographics is not None
            result.sources_consulted = sum(
                len(q) for q in queries
            )  # approximate
        else:
            result.status = "done"
            result.fees = 0
            result.amenities = 0
            result.proximity = 0
            result.demographics_present = False

    except Exception as exc:
        result.status = "error"
        result.errors.append(str(exc))
        logger.error("Browser condenser failed for %s: %s", info.name, exc)

    result.elapsed = time.monotonic() - started
    return result


async def _run_gemini_condenser(
    info: CommunityInfo,
    store: CommunityStore,
    database: CommunityDatabase,
    url_tracker: URLTracker,
    on_progress: Optional[Callable[[str], None]] = None,
) -> CondenserStepResult:
    """Run the Gemini condenser for one community.

    Returns ``skipped`` if Gemini is not configured.
    """
    started = time.monotonic()
    result = CondenserStepResult(status="running")

    # Check if Gemini is configured
    base_url = os.environ.get("MODEL_BASE_URL", "")
    api_key = os.environ.get("MODEL_API_KEY", "")
    if "generativelanguage.googleapis.com" not in base_url or not api_key:
        result.status = "skipped"
        result.errors.append("Gemini not configured (missing MODEL_BASE_URL/MODEL_API_KEY)")
        result.elapsed = time.monotonic() - started
        return result

    try:
        from .gemini_condenser import GeminiCondenser

        condenser = GeminiCondenser(store=store, database=database)
        item = await condenser.enrich_community(info.name, city=info.city)
        result.status = "done"
        result.fees = len(item.fees)
        result.amenities = len(item.amenities)
        result.proximity = len(item.proximity)
        result.demographics_present = item.demographics is not None
    except Exception as exc:
        result.status = "error"
        result.errors.append(str(exc))
        logger.error("Gemini condenser failed for %s: %s", info.name, exc)

    result.elapsed = time.monotonic() - started
    return result


# Map of condenser name → runner function
_CONDENSER_RUNNERS: dict[str, Callable] = {
    "ai": _run_ai_condenser,
    "web": _run_web_condenser,
    "research": _run_research_agent,
    "browser": _run_browser_condenser,
    "gemini": _run_gemini_condenser,
}

# Human-readable condenser names
_CONDENSER_LABELS: dict[str, str] = {
    "ai": "AI Condenser (local model knowledge)",
    "web": "Web Condenser (DuckDuckGo + local model)",
    "research": "Research Agent (deep web research)",
    "browser": "Browser Condenser (Google + Chrome + local model)",
    "gemini": "Gemini Condenser (Gemini API + web grounding)",
}


# ── Pipeline engine ──────────────────────────────────────────────────────────


class FullPipeline:
    """Runs all condenser methods against all known communities.

    The pipeline is fully resumable: state is saved after every condenser step,
    so stopping the process (Ctrl-C, crash, power loss) is safe.  On restart,
    the pipeline picks up exactly where it left off.
    """

    def __init__(
        self,
        state_path: Path | str = DEFAULT_STATE_PATH,
        store: CommunityStore | None = None,
        database: CommunityDatabase | None = None,
        condensers: list[str] | None = None,
        discovery_state_path: Path | str | None = None,
        url_tracker: URLTracker | None = None,
        on_progress: Optional[Callable[[str], None]] = None,
        on_community_start: Optional[Callable[[str, str], None]] = None,
        on_community_done: Optional[Callable[[str, str, CondenserStepResult], None]] = None,
        on_pipeline_done: Optional[Callable[[PipelineState], None]] = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.store = store or CommunityStore()
        self.database = database or CommunityDatabase()
        self.condensers = condensers or list(ALL_CONDENSERS)
        self.discovery_state_path = Path(discovery_state_path) if discovery_state_path else None
        self.url_tracker = url_tracker or URLTracker(self.database)
        self.on_progress = on_progress
        self.on_community_start = on_community_start
        self.on_community_done = on_community_done
        self.on_pipeline_done = on_pipeline_done

    def _log(self, message: str) -> None:
        if self.on_progress:
            self.on_progress(message)
        logger.info(message)

    def init_state(self, reset: bool = False) -> PipelineState:
        """Initialise or reload pipeline state."""
        if reset and self.state_path.exists():
            self.state_path.unlink()
            self._log("Pipeline state reset.")

        state = PipelineState.load(self.state_path)

        if not state.communities or reset:
            # Discover all communities from every source
            communities = _discover_all_communities(
                self.store, self.database, self.discovery_state_path
            )
            self._log(f"Discovered {len(communities)} communities from all sources")
            for c in communities:
                self._log(f"  [{c.source}] {c.name} ({c.city or '?'})")

            state.communities = communities
            state.condensers = list(self.condensers)

            # Initialise results for each community
            for ci in communities:
                if ci.slug not in state.results:
                    state.results[ci.slug] = {}
                for cond in state.condensers:
                    if cond not in state.results[ci.slug]:
                        state.results[ci.slug][cond] = CondenserStepResult()

            if not state.started_at or reset:
                state.started_at = _now_iso()
                state.stats.started_at = _now_iso()

            state.save(self.state_path)

        return state

    async def run(
        self,
        community: Optional[str] = None,
        reset: bool = False,
    ) -> PipelineState:
        """Run the full pipeline.

        Args:
            community: If set, only process communities matching this name.
            reset: If True, discard existing state and start fresh.

        Returns:
            Final pipeline state.
        """
        state = self.init_state(reset=reset)

        # Filter communities if requested
        if community is not None:
            needle = community.lower()
            target_slugs = {
                ci.slug for ci in state.communities if needle in ci.name.lower()
            }
            if not target_slugs:
                self._log(f"No communities matching '{community}'")
                return state
        else:
            target_slugs = set(state.community_slugs())

        # Compute work list: (slug, condenser) pairs still to run
        pending = state.pending_work()
        pending = [(s, c) for s, c in pending if s in target_slugs]

        total = len(pending)
        if total == 0:
            self._log("Nothing to do — all condensers have been run.")
            return state

        self._log(
            f"\n{'=' * 70}\n"
            f"FULL PIPELINE — {len(target_slugs)} communities × "
            f"{len(state.condensers)} condensers = {total} steps\n"
            f"{'=' * 70}"
        )

        done = 0
        current_slug = None

        for slug, condenser_name in pending:
            # Track community transitions
            if slug != current_slug:
                if current_slug is not None:
                    self._log("")  # blank line between communities
                current_slug = slug
                ci = next(c for c in state.communities if c.slug == slug)
                progress = state.community_progress(slug)
                done_count = sum(1 for s in progress.values() if s == "done")
                total_count = len(progress)
                self._log(
                    f"\n{'─' * 60}\n"
                    f"Community: {ci.name} [{ci.city or '?'}] "
                    f"({done_count}/{total_count} condensers done)\n"
                    f"{'─' * 60}"
                )
                if self.on_community_start:
                    self.on_community_start(slug, condenser_name)

            label = _CONDENSER_LABELS.get(condenser_name, condenser_name)
            self._log(f"  ▶ [{condenser_name}] {label}...")

            # Mark as running
            state.results[slug][condenser_name].status = "running"
            state.save(self.state_path)

            # Run the condenser
            runner = _CONDENSER_RUNNERS.get(condenser_name)
            if runner is None:
                self._log(f"  ✗ Unknown condenser: {condenser_name}")
                state.results[slug][condenser_name].status = "skipped"
                state.results[slug][condenser_name].errors.append(
                    f"Unknown condenser: {condenser_name}"
                )
                state.stats.skipped += 1
            else:
                ci = next(c for c in state.communities if c.slug == slug)
                # Ensure record exists before running
                _ensure_community_record(ci, self.store, self.database)

                step_result = await runner(
                    ci,
                    self.store,
                    self.database,
                    url_tracker=self.url_tracker,
                    on_progress=self.on_progress,
                )
                state.results[slug][condenser_name] = step_result

                # Update stats
                state.stats.total_runs += 1
                if step_result.status == "done":
                    state.stats.successful += 1
                    self._log(
                        f"  ✓ [{condenser_name}] done in {step_result.elapsed:.1f}s — "
                        f"{step_result.fees} fees, {step_result.amenities} amenities, "
                        f"demo={'yes' if step_result.demographics_present else 'no'}"
                    )
                elif step_result.status == "skipped":
                    state.stats.skipped += 1
                    self._log(
                        f"  ⊘ [{condenser_name}] skipped: {step_result.errors[0] if step_result.errors else '?'}"
                    )
                elif step_result.status == "error":
                    state.stats.failed += 1
                    self._log(
                        f"  ✗ [{condenser_name}] FAILED: {step_result.errors[0] if step_result.errors else '?'}"
                    )

                if self.on_community_done:
                    self.on_community_done(slug, condenser_name, step_result)

            # Save state after every step
            done += 1
            state.save(self.state_path)

            self._log(
                f"  [Progress: {done}/{total} steps complete]"
            )

        # Pipeline complete
        self._log(
            f"\n{'=' * 70}\n"
            f"PIPELINE COMPLETE\n"
            f"  Total runs: {state.stats.total_runs}\n"
            f"  Successful: {state.stats.successful}\n"
            f"  Failed:     {state.stats.failed}\n"
            f"  Skipped:    {state.stats.skipped}\n"
            f"{'=' * 70}"
        )

        # Print per-community summary
        self._log("\nPer-community summary:")
        for ci in state.communities:
            if ci.slug not in target_slugs:
                continue
            progress = state.community_progress(ci.slug)
            done_items = []
            for cond, status in progress.items():
                icon = {"done": "✓", "error": "✗", "skipped": "⊘", "pending": "·", "running": "▶"}.get(
                    status, "?"
                )
                done_items.append(f"{icon}{cond}")
            self._log(f"  {ci.name}: {' '.join(done_items)}")

        # Print database summary
        try:
            db_summary = self.database.summary()
            self._log(f"\nDatabase summary:")
            self._log(f"  Total communities: {db_summary.get('total_communities', '?')}")
            self._log(f"  Gated: {db_summary.get('gated_count', '?')}")
            self._log(f"  With fees: {db_summary.get('with_fees', '?')}")
            self._log(f"  With amenities: {db_summary.get('with_amenities', '?')}")
            self._log(f"  With demographics: {db_summary.get('with_demographics', '?')}")
        except Exception as exc:
            self._log(f"  (Could not get DB summary: {exc})")

        if self.on_pipeline_done:
            self.on_pipeline_done(state)

        return state


# ── CLI ──────────────────────────────────────────────────────────────────────


def _print_status(state: PipelineState) -> None:
    """Print a human-readable status report."""
    print(json.dumps({
        "started_at": state.started_at,
        "last_updated": state.last_updated,
        "communities": len(state.communities),
        "condensers": state.condensers,
        "stats": state.stats.model_dump(),
        "community_progress": {
            ci.slug: {
                "name": ci.name,
                "city": ci.city,
                "condensers": state.community_progress(ci.slug),
            }
            for ci in state.communities
        },
    }, indent=2))


async def _main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Full pipeline: discover and condense communities using all methods."
    )
    parser.add_argument(
        "--community",
        help="Only process communities matching this name (substring match).",
    )
    parser.add_argument(
        "--condensers",
        help="Comma-separated list of condensers to run (default: all). "
             "Options: ai,web,research,browser,gemini",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Discard existing state and start fresh.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print current pipeline status and exit.",
    )
    parser.add_argument(
        "--state-path",
        default=str(DEFAULT_STATE_PATH),
        help=f"Path to state file (default: {DEFAULT_STATE_PATH})",
    )
    parser.add_argument("--db-path", default="data/communities.db")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    database = CommunityDatabase(args.db_path)
    database.create_tables()

    condensers = args.condensers.split(",") if args.condensers else None

    url_tracker = URLTracker(database)

    pipeline = FullPipeline(
        state_path=Path(args.state_path),
        database=database,
        condensers=condensers,
        url_tracker=url_tracker,
        on_progress=lambda msg: print(f"[PIPELINE] {msg}"),
    )

    if args.status:
        state = pipeline.init_state()
        _print_status(state)
        return

    await pipeline.run(community=args.community, reset=args.reset)


def main(argv: Optional[list[str]] = None) -> None:
    asyncio.run(_main(argv))


if __name__ == "__main__":
    main()
