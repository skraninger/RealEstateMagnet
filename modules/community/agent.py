"""Research agent (Workstream B) — PydanticAI on a local OpenAI-compatible model.

The agent is a research assistant, not a scraper: it searches, reads the pages
it judges relevant, and returns ``CommunityFacts``. Code validates the output
before it reaches the store; every tool call is captured as a
``ResearchLogEntry`` for the audit trail.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic_ai import Agent

from .models import CommunityFacts, ResearchLogEntry
from .tools import read_page as read_page_impl
from .tools import take_screenshot as take_screenshot_impl
from .tools import analyze_screenshot as analyze_screenshot_impl
from .tools import web_search as web_search_impl

logger = logging.getLogger(__name__)


def build_model(
    model_name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> Any:
    """Build an OpenAI-compatible chat model from environment variables or parameters.

    This is a standalone helper for creating models outside of the agent class.
    Used by the browser condenser and other components that need direct model access.

    Args:
        model_name: Model identifier (default: from MODEL_NAME env var or "local")
        base_url: Base URL for the API (default: from MODEL_BASE_URL env var)
        api_key: API key (default: from MODEL_API_KEY env var or "local")

    Returns:
        An OpenAIChatModel instance configured for the specified endpoint
    """
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    return OpenAIChatModel(
        model_name or os.environ.get("MODEL_NAME", "local"),
        provider=OpenAIProvider(
            base_url=base_url or os.environ.get("MODEL_BASE_URL", "http://localhost:8080/v1"),
            api_key=api_key or os.environ.get("MODEL_API_KEY", "local"),
        ),
    )


SYSTEM_PROMPT = """\
You are a real-estate research assistant compiling information about gated \
communities in southern Florida for a potential buyer.

Research dimensions (collect everything you can find):
1. Fees — HOA dues (monthly/annual), CDD special assessments, other recurring charges.
2. Amenities — pool, clubhouse, golf, tennis, trails, fitness center, gate/security details, pet policies.
3. Demographics — median age, median household income, owner-occupancy %, population \
(prefer Census/ACS; note the data year and geography level).
4. Proximity — nearest shopping/grocery, library, hospital, school, with approximate distance in miles.

Rules:
- Use web_search to find candidate pages, then read_page to verify before recording anything.
- If read_page returns empty or very little content, the page may have data in images or complex layouts. \
In that case, use screenshot_page to capture the page, then use analyze_image to extract the information visually.
- Prefer official community/HOA/CDD sites and census sources; treat listing-site fee figures as lower confidence.
- Record ONLY facts you actually saw on a page you read. Every fact must carry the source_url of that page.
- Never fabricate or estimate numbers you did not see; leave fields null instead.
- Confidence: 0.8-1.0 official sources (HOA/CDD/census), 0.5-0.8 reputable listing/news sites, below 0.5 forums/unverified.
- If sources conflict, report all versions as separate facts — do not reconcile them yourself.
- List anything you could not verify in open_questions.
"""


@dataclass
class ResearchResult:
    facts: CommunityFacts
    log_entries: list[ResearchLogEntry] = field(default_factory=list)
    prompt: str = ""


def _make_tools(
    record_call: Callable[[dict[str, Any]], None],
    max_results: int,
    page_max_chars: int,
    enable_screenshot: bool = False,
) -> list[Callable]:
    async def web_search(query: str) -> str:
        """Search the web for information about a gated community (fees, amenities, demographics, nearby services)."""
        results = web_search_impl(query, max_results=max_results)
        record_call({"action": "search", "query": query, "url": None, "summary": f"{len(results)} results"})
        if not results:
            return "No results."
        return "\n".join(f"- {r.title} | {r.url}\n  {r.snippet}" for r in results)

    async def read_page(url: str, use_js: bool = False) -> str:
        """Read the main text content of a web page. Set use_js=True only if the page appears to require JavaScript."""
        text = await read_page_impl(url, max_chars=page_max_chars, use_js=use_js)
        record_call({"action": "read_page", "query": None, "url": url, "summary": f"{len(text)} chars"})
        return text or "(page could not be read)"

    tools = [web_search, read_page]

    if enable_screenshot:
        async def screenshot_page(url: str) -> str:
            """Take a screenshot of a web page. Use when data appears to be in images or complex layouts that text extraction misses. Returns the path to the saved PNG file."""
            path = await take_screenshot_impl(url)
            record_call({"action": "screenshot", "query": None, "url": url, "summary": f"screenshot saved to {path}" if path else "screenshot failed"})
            return path or "(screenshot failed)"

        async def analyze_image(image_path: str, question: str) -> str:
            """Analyze a screenshot using vision capabilities. Use this to extract information from images that text extraction cannot capture (e.g., data in charts, complex layouts, images). Returns the LLM's analysis as text."""
            analysis = await analyze_screenshot_impl(image_path, question)
            record_call({"action": "analyze_image", "query": question, "url": image_path, "summary": f"{len(analysis)} chars of analysis"})
            return analysis or "(analysis failed)"

        tools.extend([screenshot_page, analyze_image])

    return tools


class CommunityResearchAgent:
    """Per-community research loop (plan §6). Fully local by default."""

    def __init__(
        self,
        model: Any | None = None,
        model_name: str | None = None,
        base_url: str | None = None,
        max_results: int | None = None,
        page_max_chars: int | None = None,
        max_tokens: int | None = None,
        enable_screenshot: bool = False,
    ) -> None:
        self.model = model
        self.model_name = model_name or os.environ.get("MODEL_NAME", "local")
        self.base_url = base_url or os.environ.get(
            "MODEL_BASE_URL", "http://localhost:8080/v1"
        )
        self.max_results = max_results or int(os.environ.get("RESEARCH_MAX_RESULTS", "8"))
        self.page_max_chars = page_max_chars or int(
            os.environ.get("RESEARCH_PAGE_MAX_CHARS", "8000")
        )
        self.max_tokens = max_tokens or int(os.environ.get("MODEL_MAX_TOKENS", "8192"))
        self.enable_screenshot = enable_screenshot or os.environ.get("ENABLE_SCREENSHOT", "").lower() in ("1", "true", "yes")

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

    @staticmethod
    def build_prompt(identity: dict[str, Any]) -> str:
        lines = [f"Research the gated community '{identity['name']}'."]
        if identity.get("city"):
            lines.append(f"Location: {identity['city']}, Florida.")
        if identity.get("hoa_name"):
            lines.append(f"HOA: {identity['hoa_name']}.")
        if identity.get("cdd_name"):
            lines.append(f"CDD district: {identity['cdd_name']}.")
        if identity.get("notes"):
            lines.append(f"Notes: {identity['notes']}")
        lines.append(
            "Research its fees, amenities, demographic profile, and proximity to "
            "shopping and public services (libraries, hospitals, schools)."
        )
        return "\n".join(lines)

    async def research_community(self, identity: dict[str, Any]) -> ResearchResult:
        calls: list[dict[str, Any]] = []
        agent = Agent(
            self._build_model(),
            output_type=CommunityFacts,
            system_prompt=SYSTEM_PROMPT,
            retries=2,
            tools=_make_tools(calls.append, self.max_results, self.page_max_chars, self.enable_screenshot),
        )
        prompt = self.build_prompt(identity)
        result = await agent.run(prompt, model_settings={"max_tokens": self.max_tokens})
        facts: CommunityFacts = result.output
        slug = identity.get("slug")
        log_entries = [
            ResearchLogEntry(
                community_slug=slug,
                action=call["action"],  # type: ignore[arg-type]
                query=call.get("query"),
                url=call.get("url"),
                summary=call.get("summary"),
                model_name=self.model_name if self.model is None else "test-model",
            )
            for call in calls
        ]
        log_entries.append(
            ResearchLogEntry(
                community_slug=slug,
                action="extract",
                query=None,
                url=None,
                summary=(
                    f"{len(facts.fees)} fees, {len(facts.amenities)} amenities, "
                    f"demographics={'yes' if facts.demographics else 'no'}, "
                    f"{len(facts.proximity)} proximity metrics"
                ),
                model_name=self.model_name if self.model is None else "test-model",
            )
        )
        return ResearchResult(facts=facts, log_entries=log_entries, prompt=prompt)
