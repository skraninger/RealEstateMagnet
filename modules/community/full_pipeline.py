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
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from .agent import CommunityResearchAgent
from .database import TERMINAL_OK_STATUSES, CommunityDatabase
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

# How often the pipeline prints a "still working" heartbeat while a condenser
# runs. A single research step can take minutes, so this reassures an operator
# watching the console that the run is alive (and lets them decide whether to
# stop and resume). Set PIPELINE_HEARTBEAT_SECONDS=0 to disable.
HEARTBEAT_SECONDS = int(os.environ.get("PIPELINE_HEARTBEAT_SECONDS", "30"))

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
    state_path: Path | None = None,
) -> list[CommunityInfo]:
    """Gather communities from every known source, deduplicated by slug.

    Sources (priority order): target_list.json, discovery_state.json, the
    database, and the legacy ``pipeline_state.json`` (used once for migration).
    """
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

    # Source 3: database (lightweight identity list — no facts loaded)
    try:
        for info in database.list_community_infos():
            slug = info.get("slug")
            if slug and slug not in seen:
                seen[slug] = CommunityInfo(
                    name=info.get("name") or slug,
                    slug=slug,
                    city=info.get("city"),
                    county=info.get("county"),
                    source="database",
                )
    except Exception as exc:
        logger.warning("Could not list database communities: %s", exc)

    # Source 4: legacy pipeline_state.json (identity only). Only needed on
    # bootstrap; once the DB has communities the file is redundant, so skip it
    # to keep restarts fast.
    try:
        need_state_file = database.count() == 0
    except Exception:
        need_state_file = True
    if need_state_file and state_path is not None and Path(state_path).exists():
        try:
            data = json.loads(Path(state_path).read_text(encoding="utf-8"))
            for dc in data.get("communities", []):
                name = dc.get("name", "")
                slug = dc.get("slug") or slugify(name)
                if slug and slug not in seen:
                    seen[slug] = CommunityInfo(
                        name=name,
                        slug=slug,
                        city=dc.get("city"),
                        county=dc.get("county"),
                        source="pipeline_state",
                    )
        except Exception as exc:
            logger.warning("Could not read pipeline state: %s", exc)

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

    engine = CommunityResearchEngine(store=store, url_tracker=url_tracker)
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
            url_tracker.register_urls(
                urls, discovered_by="browser_condenser", community_slug=info.slug
            )

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
                        http_status=400,
                        community_slug=info.slug,
                    )
                    continue
                
                if not page_text:
                    # Record failure in URL tracker
                    url_tracker.update_url_status(
                        sr.url,
                        status="failed",
                        error_message="Empty page content",
                        http_status=400,
                        community_slug=info.slug,
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
                    community_slug=info.slug,
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
        # Completion is judged against the full condenser set, so a partial
        # subset run (e.g. --condensers ai) never marks a community complete.
        self.completion_condensers = list(ALL_CONDENSERS)
        self.discovery_state_path = Path(discovery_state_path) if discovery_state_path else None
        self.url_tracker = url_tracker or URLTracker(self.database)
        self.on_progress = on_progress
        self.on_community_start = on_community_start
        self.on_community_done = on_community_done
        self.on_pipeline_done = on_pipeline_done
        # Heartbeat bookkeeping: what condenser is running right now and since
        # when (monotonic seconds). Updated by run(); read by _heartbeat().
        self._current_step = ""
        self._step_started = 0.0

    def _log(self, message: str) -> None:
        if self.on_progress:
            self.on_progress(message)
        logger.info(message)

    async def _run_with_heartbeat(
        self,
        runner: Callable,
        info: CommunityInfo,
        condenser_name: str,
    ) -> CondenserStepResult:
        """Await one condenser runner, logging a periodic "still working" line.

        A single research/vision step can take several minutes with no other
        output. Without a heartbeat the console looks frozen and an operator
        cannot tell whether to keep waiting or stop and resume.
        """
        task = asyncio.ensure_future(
            runner(
                info,
                self.store,
                self.database,
                url_tracker=self.url_tracker,
                on_progress=self.on_progress,
            )
        )
        if HEARTBEAT_SECONDS <= 0:
            return await task

        self._current_step = f"{info.name} / {condenser_name}"
        self._step_started = time.monotonic()
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
                if task in done:
                    return task.result()
                elapsed = time.monotonic() - self._step_started
                self._log(
                    f"  ⏳ still working on {self._current_step} "
                    f"({elapsed:.0f}s elapsed)"
                )
        finally:
            self._current_step = ""
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    def init_state(self, reset: bool = False) -> PipelineState:
        """Load state, register every discovered community, optionally reset.

        The database is the source of truth for progress; ``pipeline_state.json``
        is kept as a human-readable mirror and one-time migration input. A reset
        clears progress but never discards community identity or collected data.
        """
        state = PipelineState.load(self.state_path)

        # Discover every known community (identity only) and merge with the
        # mirror, preserving prior order.
        discovered = _discover_all_communities(
            self.store, self.database, self.discovery_state_path, self.state_path
        )
        self._log(f"Discovered {len(discovered)} communities from all sources")

        ordered = list(state.communities)
        seen = {c.slug for c in ordered}
        for c in discovered:
            if c.slug not in seen:
                ordered.append(c)
                seen.add(c.slug)
        state.communities = ordered
        state.condensers = list(self.condensers)

        # Record every community in the database (duplicate-safe + sort order).
        n = self.database.register_communities(
            [
                {
                    "name": c.name,
                    "slug": c.slug,
                    "city": c.city,
                    "county": c.county,
                    "source": c.source,
                }
                for c in state.communities
            ]
        )
        self._log(f"Registered {n} communities in the database")

        if reset:
            self.database.reset_all_statuses()
            self.database.reset_condenser_runs()
            state.stats = PipelineStats()
            state.started_at = None
            for slug, results in state.results.items():
                for cond in list(results):
                    results[cond] = CondenserStepResult()
            self._log("Pipeline progress reset (community data retained).")

        # Initialise the reporting mirror for each community/condenser.
        for ci in state.communities:
            state.results.setdefault(ci.slug, {})
            for cond in state.condensers:
                state.results[ci.slug].setdefault(cond, CondenserStepResult())

        if not state.started_at:
            state.started_at = _now_iso()
            state.stats.started_at = _now_iso()

        state.save(self.state_path)
        return state

    async def run(
        self,
        community: Optional[str] = None,
        reset: bool = False,
    ) -> PipelineState:
        """Run the pipeline, resuming at the first community not completed.

        Communities are processed in ``community_pipeline_status.sort_order``.
        Each is flagged ``processing`` while its condensers run and
        ``completed`` once every condenser in ``self.completion_condensers`` has
        finished (``done`` or ``skipped``). Every URL inspected for a community
        is linked to it in the ``community_urls`` table.

        Args:
            community: If set, only process communities matching this name.
            reset: If True, clear progress (data retained) and start fresh.

        Returns:
            Final pipeline state (reporting mirror).
        """
        state = self.init_state(reset=reset)

        # Recover communities interrupted mid-run so they resume cleanly.
        stale = self.database.reset_stale_processing()
        if stale:
            self._log(f"Recovered {stale} interrupted community(ies) for resume.")

        # Ordered target list (from the database, the source of truth).
        statuses = self.database.list_pipeline_statuses()
        if community is not None:
            needle = community.lower()
            statuses = [
                s
                for s in statuses
                if needle in s["name"].lower() or needle in s["slug"]
            ]
            if not statuses:
                self._log(f"No communities matching '{community}'")
                return state

        # Only communities not yet completed, in order — this starts at the
        # first community that has not been completed.
        pending_targets = [s for s in statuses if s["status"] != "completed"]
        target_slugs = {s["slug"] for s in statuses}

        if not pending_targets:
            self._log("Nothing to do — all target communities are completed.")
            return state

        # Preload existing condenser statuses for the pending communities in a
        # single query (avoids an N+1 scan on every restart).
        statuses_map = self.database.condenser_statuses_map(
            [t["slug"] for t in pending_targets]
        )

        # Total condenser steps remaining across the pending communities.
        total_steps = 0
        for target in pending_targets:
            runs = statuses_map.get(target["slug"], {})
            total_steps += sum(
                1 for c in state.condensers if runs.get(c) not in TERMINAL_OK_STATUSES
            )

        self._log(
            f"\n{'=' * 70}\n"
            f"FULL PIPELINE — {len(pending_targets)} community(ies) to process "
            f"(starting at: {pending_targets[0]['name']})\n"
            f"Condensers: {', '.join(state.condensers)} "
            f"({total_steps} steps remaining this run)\n"
            f"{'=' * 70}"
        )

        done = 0
        ci_by_slug = {c.slug: c for c in state.communities}

        for target in pending_targets:
            slug = target["slug"]
            ci = ci_by_slug.get(slug)
            if ci is None:
                ci = CommunityInfo(
                    name=target["name"],
                    slug=slug,
                    city=target["city"],
                    source="database",
                )
                state.communities.append(ci)
                ci_by_slug[slug] = ci

            # Ensure the filesystem record exists, then flag the community.
            _ensure_community_record(ci, self.store, self.database)
            self.database.mark_processing(slug)

            runs = statuses_map.setdefault(slug, {})
            completed_count = sum(
                1 for v in runs.values() if v in TERMINAL_OK_STATUSES
            )
            self._log(
                f"\n{'─' * 60}\n"
                f"Community: {ci.name} [{ci.city or '?'}] "
                f"({completed_count}/{len(self.completion_condensers)} condensers done)\n"
                f"{'─' * 60}"
            )
            if self.on_community_start:
                self.on_community_start(slug, "")

            for condenser_name in state.condensers:
                # Resume: skip condensers already finished on a prior run.
                if runs.get(condenser_name) in TERMINAL_OK_STATUSES:
                    self._log(f"  ↻ [{condenser_name}] already done — skipping")
                    continue

                label = _CONDENSER_LABELS.get(condenser_name, condenser_name)
                self._log(f"  ▶ [{condenser_name}] {label}...")

                state.results.setdefault(slug, {})[
                    condenser_name
                ] = CondenserStepResult(status="running")

                runner = _CONDENSER_RUNNERS.get(condenser_name)
                if runner is None:
                    step_result = CondenserStepResult(status="skipped")
                    step_result.errors.append(f"Unknown condenser: {condenser_name}")
                else:
                    step_result = await self._run_with_heartbeat(
                        runner, ci, condenser_name
                    )

                # Persist to the database (authoritative) + reporting mirror.
                self.database.record_condenser_run(slug, condenser_name, step_result)
                state.results[slug][condenser_name] = step_result
                runs[condenser_name] = step_result.status

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
                        f"  ⊘ [{condenser_name}] skipped: "
                        f"{step_result.errors[0] if step_result.errors else '?'}"
                    )
                elif step_result.status == "error":
                    state.stats.failed += 1
                    self._log(
                        f"  ✗ [{condenser_name}] FAILED: "
                        f"{step_result.errors[0] if step_result.errors else '?'}"
                    )

                if self.on_community_done:
                    self.on_community_done(slug, condenser_name, step_result)

                done += 1
                self._log(f"  [Progress: {done}/{total_steps} steps this run]")

            # Completion is judged across the full condenser set (from the
            # in-memory status map, updated as runs finish).
            if all(
                runs.get(c) in TERMINAL_OK_STATUSES
                for c in self.completion_condensers
            ):
                self.database.mark_completed(slug)
                self._log(f"  ✓ Community complete: {ci.name}")
            else:
                incomplete = [
                    c
                    for c in self.completion_condensers
                    if runs.get(c) not in TERMINAL_OK_STATUSES
                ]
                self.database.mark_partial(
                    slug, error=f"pending condensers: {', '.join(incomplete)}"
                )
                self._log(
                    f"  • Community partial: {ci.name} "
                    f"(pending: {', '.join(incomplete)})"
                )

        # Persist the JSON reporting mirror once per run (the database is
        # authoritative and is written after every step, so no per-step mirror
        # writes are needed — this keeps long runs and restarts fast).
        state.save(self.state_path)

        # Pipeline complete
        self._log(
            f"\n{'=' * 70}\n"
            f"PIPELINE RUN COMPLETE\n"
            f"  Total runs: {state.stats.total_runs}\n"
            f"  Successful: {state.stats.successful}\n"
            f"  Failed:     {state.stats.failed}\n"
            f"  Skipped:    {state.stats.skipped}\n"
            f"{'=' * 70}"
        )

        # Per-community summary (from the database, filtered to targets).
        self._log("\nPer-community summary:")
        final_statuses = {
            s["slug"]: s for s in self.database.list_pipeline_statuses()
        }
        for slug in [s["slug"] for s in statuses]:
            info = final_statuses.get(slug)
            if info is None:
                continue
            runs = self.database.get_condenser_runs(slug)
            marks = []
            for cond in self.completion_condensers:
                st = runs.get(cond, {}).get("status", "pending")
                icon = {
                    "done": "✓",
                    "error": "✗",
                    "skipped": "⊘",
                    "pending": "·",
                    "running": "▶",
                }.get(st, "?")
                marks.append(f"{icon}{cond}")
            self._log(f"  {info['name']}: {' '.join(marks)}  [{info['status']}]")

        # Database summary
        try:
            db_summary = self.database.summary()
            self._log("\nDatabase summary:")
            self._log(f"  Total communities: {db_summary.get('total_communities', '?')}")
            self._log(f"  Gated: {db_summary.get('gated_count', '?')}")
            self._log(f"  With fees: {db_summary.get('with_fees', '?')}")
            self._log(f"  With amenities: {db_summary.get('with_amenities', '?')}")
            self._log(f"  With demographics: {db_summary.get('with_demographics', '?')}")
            statuses_summary = db_summary.get("pipeline_statuses") or {}
            if statuses_summary:
                self._log(
                    "  Pipeline status: "
                    + ", ".join(f"{k}={v}" for k, v in statuses_summary.items())
                )
            self._log(f"  URLs linked to communities: {db_summary.get('linked_urls', '?')}")
        except Exception as exc:
            self._log(f"  (Could not get DB summary: {exc})")

        # Keep target_slugs referenced for clarity in logs (unused otherwise).
        logger.debug("targets: %s", target_slugs)

        if self.on_pipeline_done:
            self.on_pipeline_done(state)

        return state


# ── CLI ──────────────────────────────────────────────────────────────────────


def _print_status(
    database: CommunityDatabase,
    state: Optional[PipelineState] = None,
) -> None:
    """Print a progress report from the database (source of truth)."""
    statuses = database.list_pipeline_statuses()
    statuses_map = database.condenser_statuses_map()
    status_counts: dict[str, int] = {}
    community_progress: dict[str, Any] = {}
    for s in statuses:
        status_counts[s["status"]] = status_counts.get(s["status"], 0) + 1
        runs = statuses_map.get(s["slug"], {})
        community_progress[s["slug"]] = {
            "name": s["name"],
            "city": s["city"],
            "status": s["status"],
            "sort_order": s["sort_order"],
            "condensers": {
                c: runs.get(c, "pending") for c in ALL_CONDENSERS
            },
        }

    print(json.dumps({
        "started_at": state.started_at if state else None,
        "last_updated": _now_iso(),
        "communities": len(statuses),
        "condensers": list(ALL_CONDENSERS),
        "status_counts": status_counts,
        "next_incomplete": database.next_incomplete_community(),
        "stats": state.stats.model_dump() if state else {},
        "community_progress": community_progress,
    }, indent=2))


def _force_utf8_stdio() -> None:
    """Make stdout/stderr tolerate the pipeline's Unicode log glyphs.

    The Windows console is often cp1252, which cannot encode the box-drawing
    characters (``─``) and status icons (``▶ ✓ ✗``) the pipeline logs. Without
    this, ``print`` raises ``UnicodeEncodeError`` and the whole run aborts with
    exit code 1. ``errors="replace"`` keeps logging non-fatal on any terminal.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - non-TTY/old
            pass


async def _main(argv: Optional[list[str]] = None) -> None:
    _force_utf8_stdio()
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

    # One-time (idempotent) migration of legacy data into the new schema.
    try:
        from .migration import migrate_database

        migrate_database(
            db=database,
            state_path=Path(args.state_path),
            verbose=True,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logging.getLogger(__name__).warning("Migration skipped: %s", exc)

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
        _print_status(database, state)
        return

    await pipeline.run(community=args.community, reset=args.reset)


def main(argv: Optional[list[str]] = None) -> None:
    asyncio.run(_main(argv))


if __name__ == "__main__":
    main()
