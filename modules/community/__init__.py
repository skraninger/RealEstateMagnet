"""Workstream B — AI research assistant for gated communities (see FSD §2b)."""

from .agent import CommunityResearchAgent, ResearchResult, build_model
from .ai_condenser import AICondenser, CondenserResult
from .browser_condenser import BrowserCondenser, BrowserCondenserConfig
from .database import (
    CommunityCondenserRunRow,
    CommunityDatabase,
    CommunityPipelineStatusRow,
    CommunityURLRow,
    SourceURLRow,
)
from .discovery import (
    CommunityDiscoveryEngine,
    DiscoveredCommunity,
    DiscoveryResult,
)
from .gemini_condenser import GeminiCondenser, GeminiCondenserResult
from .models import (
    AI_SOURCE_URL,
    AmenityFact,
    BROWSER_SOURCE_URL,
    BrowserAction,
    BrowserCondenserState,
    BrowserExtractResult,
    CommunityFacts,
    CommunityIdentity,
    CommunityRecord,
    CondensedCommunityBatch,
    CondensedCommunityItem,
    Demographics,
    DiscoveredCommunityItem,
    Discrepancy,
    FeeFact,
    FLORIDA_COUNTIES,
    GapAnalysis,
    GoogleSearchResult,
    NextQuery,
    PageExtractionResult,
    ProximityMetric,
    ResearchLogEntry,
    VerificationResult,
)
from .research_engine import CommunityResearchEngine, CommunityResearchReport
from .store import CommunityStore, merge_facts, slugify
from .full_pipeline import (
    ALL_CONDENSERS,
    CondenserStepResult,
    CommunityInfo,
    FullPipeline,
    PipelineState,
    PipelineStats,
)
from .web_condenser import WebAICondenser, WebCondenserResult
from .url_tracker import URLTracker
from .vision_browser_agent import access_and_extract
from .migration import MigrationReport, migrate_database

__all__ = [
    "build_model",
    "AI_SOURCE_URL",
    "AICondenser",
    "AmenityFact",
    "BROWSER_SOURCE_URL",
    "BrowserAction",
    "BrowserCondenser",
    "BrowserCondenserConfig",
    "BrowserCondenserState",
    "BrowserExtractResult",
    "CommunityCondenserRunRow",
    "CommunityDatabase",
    "CommunityDiscoveryEngine",
    "CommunityFacts",
    "CommunityIdentity",
    "CommunityPipelineStatusRow",
    "CommunityRecord",
    "CommunityResearchAgent",
    "CommunityResearchEngine",
    "CommunityResearchReport",
    "CommunityStore",
    "CommunityURLRow",
    "CondensedCommunityBatch",
    "CondensedCommunityItem",
    "CondenserResult",
    "Demographics",
    "DiscoveredCommunity",
    "DiscoveredCommunityItem",
    "DiscoveryResult",
    "Discrepancy",
    "FLORIDA_COUNTIES",
    "FeeFact",
    "GapAnalysis",
    "GeminiCondenser",
    "GeminiCondenserResult",
    "GoogleSearchResult",
    "MigrationReport",
    "NextQuery",
    "PageExtractionResult",
    "ProximityMetric",
    "ResearchLogEntry",
    "ResearchResult",
    "SourceURLRow",
    "URLTracker",
    "VerificationResult",
    "WebAICondenser",
    "WebCondenserResult",
    "access_and_extract",
    "merge_facts",
    "migrate_database",
    "slugify",
    "ALL_CONDENSERS",
    "CondenserStepResult",
    "CommunityInfo",
    "FullPipeline",
    "PipelineState",
    "PipelineStats",
]
