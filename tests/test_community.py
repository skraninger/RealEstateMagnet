"""Offline tests for Workstream B (community research agent + store).

No network and no model server required: search is monkeypatched, HTTP is
mocked with pytest-httpx, and the agent runs against pydantic-ai's TestModel.
"""

from __future__ import annotations

import json
from pathlib import Path

import ddgs
import pytest
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel

from modules.community.agent import CommunityResearchAgent
from modules.community.models import (
    AmenityFact,
    CommunityFacts,
    CommunityIdentity,
    CommunityRecord,
    Demographics,
    FeeFact,
    ProximityMetric,
)
from modules.community.store import CommunityStore, merge_facts, slugify
from modules.community import tools as community_tools

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── models ───────────────────────────────────────────────────────────────────


class TestModels:
    def test_fee_fact_valid(self):
        fee = FeeFact(fee_type="hoa_monthly", amount=450.0, source_url="https://x.example.com/a")
        assert fee.period is None
        assert fee.currency == "USD"
        assert 0.0 <= fee.confidence <= 1.0

    def test_fee_fact_rejects_non_http_source(self):
        with pytest.raises(ValidationError):
            FeeFact(fee_type="hoa_monthly", source_url="ftp://x.example.com/a")

    def test_confidence_bounds_enforced(self):
        with pytest.raises(ValidationError):
            FeeFact(fee_type="other", source_url="https://x.example.com/a", confidence=1.5)

    def test_amenity_key_normalized(self):
        amenity = AmenityFact(amenity="Golf Course!!", source_url="https://x.example.com/a")
        assert amenity.amenity == "golf_course"

    def test_community_facts_defaults_empty(self):
        facts = CommunityFacts()
        assert facts.fees == [] and facts.amenities == [] and facts.proximity == []
        assert facts.demographics is None
        assert facts.open_questions == []


class TestSlugify:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Pelican Bay", "pelican-bay"),
            ("On Top of the World", "on-top-of-the-world"),
            ("  Kings Point  ", "kings-point"),
            ("!!!", "unknown"),
        ],
    )
    def test_slugify(self, raw: str, expected: str):
        assert slugify(raw) == expected


# ── store ────────────────────────────────────────────────────────────────────

SEED = {"name": "Pelican Bay", "city": "Key Biscayne", "county_fips": "12086"}


class TestStore:
    def test_upsert_identity_creates_record(self, tmp_path):
        store = CommunityStore(tmp_path)
        record = store.upsert_identity(SEED)
        assert record.identity.slug == "pelican-bay"
        assert record.identity.city == "Key Biscayne"
        assert record.fees == []

    def test_upsert_identity_returns_existing(self, tmp_path):
        store = CommunityStore(tmp_path)
        first = store.upsert_identity(SEED)
        store.save_community(first)
        second = store.upsert_identity({**SEED, "city": "Changed"})
        assert second.id == first.id
        assert second.identity.city == "Key Biscayne"

    def test_save_and_load_roundtrip(self, tmp_path):
        store = CommunityStore(tmp_path)
        record = store.upsert_identity(SEED)
        path = store.save_community(record)
        assert path.exists()
        loaded = store.load_community("pelican-bay")
        assert loaded is not None
        assert loaded.id == record.id
        assert loaded.identity.name == "Pelican Bay"

    def test_load_missing_returns_none(self, tmp_path):
        assert CommunityStore(tmp_path).load_community("nope") is None

    def test_research_log_append_and_read(self, tmp_path):
        from modules.community.models import ResearchLogEntry

        store = CommunityStore(tmp_path)
        entries = [
            ResearchLogEntry(community_slug="pelican-bay", action="search", query="fees"),
            ResearchLogEntry(community_slug="pelican-bay", action="read_page", url="https://x.example.com"),
        ]
        store.append_research_log(entries)
        loaded = store.read_research_log()
        assert len(loaded) == 2
        assert loaded[0].query == "fees"
        assert loaded[1].url == "https://x.example.com"

    def test_committed_target_list_has_at_least_ten_seeds(self):
        path = REPO_ROOT / "data" / "communities" / "target_list.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        seeds = data["communities"]
        assert len(seeds) >= 10
        for seed in seeds:
            assert seed["name"].strip()
            assert slugify(seed["name"]) != "unknown"


# ── merge / conflict logic ───────────────────────────────────────────────────


def _record(tmp_path) -> CommunityStore:
    store = CommunityStore(tmp_path)
    return store.upsert_identity(SEED)


class TestMergeFacts:
    def test_fee_conflict_flagged_and_both_kept(self, tmp_path):
        record = _record(tmp_path)
        facts_a = CommunityFacts(
            fees=[FeeFact(fee_type="hoa_monthly", amount=450.0, period="month", source_url="https://a.example.com")]
        )
        facts_b = CommunityFacts(
            fees=[FeeFact(fee_type="hoa_monthly", amount=525.0, period="month", source_url="https://b.example.com")]
        )
        merge_facts(record, facts_a)
        discrepancies = merge_facts(record, facts_b)
        assert len(record.fees) == 2
        assert [d.field for d in discrepancies] == ["fees.hoa_monthly"]
        assert {v["amount"] for v in discrepancies[0].values} == {450.0, 525.0}

    def test_duplicate_fact_dropped(self, tmp_path):
        record = _record(tmp_path)
        fee = FeeFact(fee_type="hoa_monthly", amount=450.0, period="month", source_url="https://a.example.com")
        merge_facts(record, CommunityFacts(fees=[fee]))
        discrepancies = merge_facts(record, CommunityFacts(fees=[fee.model_copy()]))
        assert len(record.fees) == 1
        assert discrepancies == []

    def test_demographics_latest_wins_with_history(self, tmp_path):
        from datetime import datetime, timezone

        record = _record(tmp_path)
        older = Demographics(
            median_age=50.0, source_url="https://old.example.com",
            retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        newer = Demographics(
            median_age=52.0, source_url="https://new.example.com",
            retrieved_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        merge_facts(record, CommunityFacts(demographics=newer))
        merge_facts(record, CommunityFacts(demographics=older))
        assert record.demographics.source_url == "https://new.example.com"
        assert [d.source_url for d in record.demographics_history] == ["https://old.example.com"]

    def test_proximity_conflict_flagged(self, tmp_path):
        record = _record(tmp_path)
        merge_facts(
            record,
            CommunityFacts(proximity=[ProximityMetric(category="library", nearest_name="Lib A", distance_miles=1.0, source_url="https://a.example.com")]),
        )
        discrepancies = merge_facts(
            record,
            CommunityFacts(proximity=[ProximityMetric(category="library", nearest_name="Lib B", distance_miles=2.0, source_url="https://b.example.com")]),
        )
        assert [d.field for d in discrepancies] == ["proximity.library"]
        assert len(record.proximity) == 2

    def test_open_questions_appended_uniquely(self, tmp_path):
        record = _record(tmp_path)
        facts = CommunityFacts(open_questions=["CDD schedule?", "  ", "CDD schedule?"])
        merge_facts(record, facts)
        merge_facts(record, CommunityFacts(open_questions=["CDD schedule?", "Gate hours?"]))
        assert record.open_questions == ["CDD schedule?", "Gate hours?"]


# ── tools ────────────────────────────────────────────────────────────────────

ARTICLE_HTML = """
<html><head><title>Pelican Bay Community</title></head>
<body>
<article>
<h1>Pelican Bay HOA</h1>
<p>The monthly HOA assessment for Pelican Bay homes is $450.00 per month.</p>
<p>Amenities include a pool, clubhouse and marina docks.</p>
</article>
</body></html>
"""


class _FakeDDGS:
    def __init__(self, rows=None, exc=None):
        self._rows = rows or []
        self._exc = exc

    def text(self, query, max_results=8):
        if self._exc is not None:
            raise self._exc
        return self._rows[:max_results]


class TestTools:
    def test_web_search_returns_results(self, monkeypatch):
        rows = [
            {"href": "https://a.example.com", "title": "A", "body": "snippet a"},
            {"href": "https://b.example.com", "title": "B", "body": "snippet b"},
            {"title": "no url row"},
        ]
        monkeypatch.setattr(ddgs, "DDGS", lambda: _FakeDDGS(rows=rows))
        results = community_tools.web_search("pelican bay hoa fees", max_results=8)
        assert [r.url for r in results] == ["https://a.example.com", "https://b.example.com"]

    def test_web_search_error_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ddgs, "DDGS", lambda: _FakeDDGS(exc=RuntimeError("rate limited")))
        assert community_tools.web_search("anything") == []

    @pytest.mark.asyncio
    async def test_read_page_extracts_text(self, httpx_mock):
        httpx_mock.add_response(url="https://a.example.com/page", text=ARTICLE_HTML)
        text = await community_tools.read_page("https://a.example.com/page", max_chars=12000)
        assert "monthly HOA assessment" in text

    @pytest.mark.asyncio
    async def test_read_page_truncates(self, httpx_mock):
        httpx_mock.add_response(url="https://a.example.com/page", text=ARTICLE_HTML)
        text = await community_tools.read_page("https://a.example.com/page", max_chars=20)
        assert len(text) <= 20

    @pytest.mark.asyncio
    async def test_read_page_failure_returns_empty(self, httpx_mock):
        httpx_mock.add_response(url="https://down.example.com", status_code=500)
        assert await community_tools.read_page("https://down.example.com") == ""

    @pytest.mark.asyncio
    async def test_model_server_health_true(self, httpx_mock):
        httpx_mock.add_response(url="http://localhost:8080/health", status_code=200)
        assert await community_tools.model_server_health("http://localhost:8080/v1") is True

    @pytest.mark.asyncio
    async def test_model_server_health_false(self, httpx_mock):
        httpx_mock.add_response(url="http://localhost:8080/health", status_code=503)
        httpx_mock.add_response(url="http://localhost:8080/v1/models", status_code=503)
        assert await community_tools.model_server_health("http://localhost:8080/v1") is False


# ── agent (TestModel, no server) ─────────────────────────────────────────────

VALID_FACTS = {
    "fees": [
        {
            "fee_type": "hoa_monthly",
            "amount": 450.0,
            "period": "month",
            "source_url": "https://hoa.example.com/fees",
            "confidence": 0.9,
        }
    ],
    "amenities": [
        {"amenity": "Golf Course", "detail": "18 holes", "source_url": "https://hoa.example.com/amen"}
    ],
    "demographics": {
        "median_age": 52.0,
        "median_household_income": 120000.0,
        "owner_occupancy_pct": 91.0,
        "population": 3000,
        "data_year": 2023,
        "source_url": "https://census.example.com",
    },
    "proximity": [
        {"category": "library", "nearest_name": "Test Library", "distance_miles": 2.5,
         "source_url": "https://lib.example.com"}
    ],
    "open_questions": ["CDD assessment schedule unconfirmed"],
}


class TestAgent:
    def test_build_prompt_contains_identity(self):
        prompt = CommunityResearchAgent.build_prompt(
            {"name": "Pelican Bay", "city": "Key Biscayne", "hoa_name": "PB HOA"}
        )
        assert "Pelican Bay" in prompt and "Key Biscayne" in prompt and "PB HOA" in prompt

    @pytest.mark.asyncio
    async def test_research_community_happy_path(self):
        agent = CommunityResearchAgent(model=TestModel(custom_output_args=VALID_FACTS))
        result = await agent.research_community({"name": "Pelican Bay", "slug": "pelican-bay"})
        facts = result.facts
        assert facts.fees[0].fee_type == "hoa_monthly"
        assert facts.amenities[0].amenity == "golf_course"
        assert facts.demographics is not None and facts.demographics.data_year == 2023
        assert facts.proximity[0].nearest_name == "Test Library"
        assert result.log_entries[-1].action == "extract"
        assert result.log_entries[-1].community_slug == "pelican-bay"
        assert "Pelican Bay" in result.prompt

    @pytest.mark.asyncio
    async def test_research_community_invalid_output_raises(self):
        agent = CommunityResearchAgent(model=TestModel(custom_output_text="this is not json"))
        with pytest.raises(Exception):
            await agent.research_community({"name": "Pelican Bay", "slug": "pelican-bay"})


# ── engine (dry run, no network) ─────────────────────────────────────────────


class TestEngine:
    @pytest.mark.asyncio
    async def test_dry_run_lists_targets_without_network(self, tmp_path):
        from modules.community.research_engine import CommunityResearchEngine

        store = CommunityStore(tmp_path)
        (tmp_path / "target_list.json").write_text(
            json.dumps({"communities": [SEED, {"name": "Kings Point", "county_fips": "12099"}]}),
            encoding="utf-8",
        )
        engine = CommunityResearchEngine(store=store)
        reports = await engine.run(dry_run=True)
        assert [r.community for r in reports] == ["Pelican Bay", "Kings Point"]
        assert all(r.dry_run for r in reports)

    @pytest.mark.asyncio
    async def test_dry_run_community_filter(self, tmp_path):
        from modules.community.research_engine import CommunityResearchEngine

        store = CommunityStore(tmp_path)
        (tmp_path / "target_list.json").write_text(
            json.dumps({"communities": [SEED, {"name": "Kings Point", "county_fips": "12099"}]}),
            encoding="utf-8",
        )
        engine = CommunityResearchEngine(store=store)
        reports = await engine.run(community="kings", dry_run=True)
        assert [r.community for r in reports] == ["Kings Point"]

    @pytest.mark.asyncio
    async def test_unknown_community_exits(self, tmp_path):
        from modules.community.research_engine import CommunityResearchEngine

        store = CommunityStore(tmp_path)
        (tmp_path / "target_list.json").write_text(
            json.dumps({"communities": [SEED]}), encoding="utf-8"
        )
        engine = CommunityResearchEngine(store=store)
        with pytest.raises(SystemExit):
            await engine.run(community="does-not-exist")


# ── discovery ─────────────────────────────────────────────────────────────────


class TestDiscoveryModels:
    def test_discovered_community_item_valid(self):
        from modules.community.models import DiscoveredCommunityItem

        item = DiscoveredCommunityItem(
            name="Sunset Ridge",
            city="Aventura",
            county="miami-dade",
            is_gated=True,
            confidence=0.8,
            evidence="Listed as gated community",
        )
        assert item.name == "Sunset Ridge"
        assert item.confidence == 0.8

    def test_page_extraction_result_valid(self):
        from modules.community.models import DiscoveredCommunityItem, PageExtractionResult

        result = PageExtractionResult(
            communities=[
                DiscoveredCommunityItem(name="Community A", confidence=0.9),
                DiscoveredCommunityItem(name="Community B", confidence=0.7),
            ],
            page_topic="community directory",
            is_relevant=True,
        )
        assert len(result.communities) == 2
        assert result.is_relevant is True

    def test_verification_result_valid(self):
        from modules.community.models import VerificationResult

        result = VerificationResult(
            is_real_community=True,
            is_gated=True,
            is_in_target_area=True,
            reasoning="Confirmed via multiple sources",
        )
        assert result.is_real_community is True
        assert result.is_in_target_area is True


class TestDiscoveredCommunity:
    def test_to_dict(self):
        from modules.community.discovery import DiscoveredCommunity

        comm = DiscoveredCommunity(
            name="Test Community",
            source_url="https://example.com",
            source_name="Example",
            city="Miami",
            county="miami-dade",
            confidence=0.7,
            is_gated=True,
            evidence="Found in directory",
        )
        d = comm.to_dict()
        assert d["name"] == "Test Community"
        assert d["city"] == "Miami"
        assert d["county"] == "miami-dade"
        assert d["confidence"] == 0.7
        assert d["is_gated"] is True

    def test_from_llm_item(self):
        from modules.community.discovery import DiscoveredCommunity
        from modules.community.models import DiscoveredCommunityItem

        item = DiscoveredCommunityItem(
            name="Pelican Bay",
            city="Naples",
            county="collier",
            is_gated=True,
            confidence=0.9,
            evidence="Official HOA site",
        )
        comm = DiscoveredCommunity.from_llm_item(item, "https://example.com", "Example")
        assert comm.name == "Pelican Bay"
        assert comm.city == "Naples"
        assert comm.confidence == 0.9


class TestCommunityDiscoveryEngine:
    def test_init_defaults(self):
        from modules.community.discovery import CommunityDiscoveryEngine

        engine = CommunityDiscoveryEngine()
        assert len(engine.seed_queries) > 0
        assert engine.model_name == "local"

    def test_init_custom(self):
        from modules.community.discovery import CommunityDiscoveryEngine

        engine = CommunityDiscoveryEngine(
            seed_queries=["test query"],
            max_results_per_query=5,
        )
        assert engine.seed_queries == ["test query"]
        assert engine.max_results_per_query == 5

    def test_save_target_list(self, tmp_path):
        from modules.community.discovery import CommunityDiscoveryEngine, DiscoveredCommunity

        engine = CommunityDiscoveryEngine()
        communities = [
            DiscoveredCommunity(
                name="Verified Community",
                source_url="https://example.com",
                source_name="Example",
                city="Miami",
                verified=True,
            ),
            DiscoveredCommunity(
                name="Unverified Community",
                source_url="https://example.com",
                source_name="Example",
                verified=False,
            ),
        ]
        output_path = tmp_path / "target_list.json"
        engine.save_target_list(communities, output_path)

        data = json.loads(output_path.read_text(encoding="utf-8"))
        assert len(data["communities"]) == 1
        assert data["communities"][0]["name"] == "Verified Community"

    @pytest.mark.asyncio
    async def test_extract_communities_with_test_model(self):
        from modules.community.discovery import CommunityDiscoveryEngine

        mock_output = {
            "communities": [
                {
                    "name": "Sunset Ridge Estates",
                    "city": "Aventura",
                    "county": "miami-dade",
                    "is_gated": True,
                    "confidence": 0.9,
                    "evidence": "Listed as gated",
                },
                {
                    "name": "Palm Lakes",
                    "city": "Boca Raton",
                    "county": "palm beach",
                    "is_gated": True,
                    "confidence": 0.8,
                    "evidence": "HOA community",
                },
            ],
            "page_topic": "community directory",
            "is_relevant": True,
            "notes": [],
        }
        from pydantic_ai.models.test import TestModel

        engine = CommunityDiscoveryEngine(model=TestModel(custom_output_args=mock_output))
        # Provide enough text to avoid vision fallback
        long_text = "Test page content about communities. " * 10
        communities = await engine.extract_communities_from_page(
            long_text,
            "https://example.com",
            "Example Page",
        )
        assert len(communities) == 2
        assert communities[0].name == "Sunset Ridge Estates"
        assert communities[1].name == "Palm Lakes"

    @pytest.mark.asyncio
    async def test_verify_community_with_test_model(self):
        from modules.community.discovery import CommunityDiscoveryEngine, DiscoveredCommunity
        from pydantic_ai.models.test import TestModel
        from unittest import mock

        mock_output = {
            "is_real_community": True,
            "is_gated": True,
            "is_in_target_area": True,
            "correct_name": None,
            "correct_location": None,
            "reasoning": "Confirmed as real gated community in Miami-Dade",
        }

        engine = CommunityDiscoveryEngine(model=TestModel(custom_output_args=mock_output))
        community = DiscoveredCommunity(
            name="Test Community",
            source_url="https://example.com",
            source_name="Example",
            city="Miami",
        )

        with mock.patch("modules.community.discovery.web_search") as mock_search, \
             mock.patch("modules.community.discovery.read_page") as mock_read:
            mock_search.return_value = [
                type("SR", (), {"url": "https://verify.com", "title": "Verify", "snippet": "test"})()
            ]
            mock_read.return_value = "Page content about Test Community in Miami"

            result = await engine.verify_community(community)

        assert result.verified is True
        assert result.confidence == 0.9


class TestDiscoveryResult:
    def test_to_dict(self):
        from modules.community.discovery import DiscoveryResult, DiscoveredCommunity

        result = DiscoveryResult(
            communities=[
                DiscoveredCommunity(name="A", source_url="u", source_name="s", verified=True),
                DiscoveredCommunity(name="B", source_url="u", source_name="s", verified=False),
            ],
            sources_consulted=["url1", "url2"],
            queries_used=["query1"],
        )
        d = result.to_dict()
        assert d["total_discovered"] == 2
        assert d["verified_count"] == 1
        assert len(d["sources_consulted"]) == 2


# ── vision capabilities ─────────────────────────────────────────────────────


class TestVisionTools:
    @pytest.mark.asyncio
    async def test_analyze_screenshot_missing_file(self):
        """analyze_screenshot should return empty string for missing file."""
        from modules.community.tools import analyze_screenshot
        
        result = await analyze_screenshot("/nonexistent/path/image.png", "test prompt")
        assert result == ""

    @pytest.mark.asyncio
    async def test_analyze_screenshot_with_mock(self, tmp_path, monkeypatch):
        """analyze_screenshot should call the model server with image data."""
        from modules.community.tools import analyze_screenshot
        import base64
        
        # Create a test image file
        test_image = tmp_path / "test.png"
        test_image.write_bytes(b"fake png data")
        
        # Mock the httpx client
        mock_response_data = {
            "choices": [
                {
                    "message": {
                        "content": "Analysis: Found 3 communities in the screenshot"
                    }
                }
            ]
        }
        
        class MockResponse:
            def __init__(self, data):
                self._data = data
            
            def raise_for_status(self):
                pass
            
            def json(self):
                return self._data
        
        async def mock_post(*args, **kwargs):
            # Verify the request contains image data
            assert "messages" in kwargs["json"]
            content = kwargs["json"]["messages"][0]["content"]
            assert any(item["type"] == "image_url" for item in content)
            return MockResponse(mock_response_data)
        
        class MockAsyncClient:
            def __init__(self, *args, **kwargs):
                pass
            
            async def __aenter__(self):
                return self
            
            async def __aexit__(self, *args):
                pass
            
            async def post(self, *args, **kwargs):
                return await mock_post(*args, **kwargs)
        
        monkeypatch.setattr("httpx.AsyncClient", MockAsyncClient)
        
        result = await analyze_screenshot(str(test_image), "Analyze this page")
        assert "Found 3 communities" in result

    @pytest.mark.asyncio
    async def test_extract_communities_from_screenshot(self, tmp_path, monkeypatch):
        """extract_communities_from_screenshot should use vision to extract communities."""
        from modules.community.discovery import CommunityDiscoveryEngine
        import asyncio
        
        # Mock take_screenshot
        async def mock_take_screenshot(*args, **kwargs):
            return str(tmp_path / "screenshot.png")
        
        # Mock analyze_screenshot
        async def mock_analyze_screenshot(*args, **kwargs):
            return """Community: Sunset Ridge Estates
City: Aventura
County: Miami-Dade
Gated: Yes
Confidence: 0.9
Evidence: Visible gate entrance in screenshot

Community: Palm Lakes Village
City: Boca Raton
County: Palm Beach
Gated: Yes
Confidence: 0.8
Evidence: Community sign visible"""
        
        monkeypatch.setattr("modules.community.discovery.take_screenshot", mock_take_screenshot)
        monkeypatch.setattr("modules.community.discovery.analyze_screenshot", mock_analyze_screenshot)
        
        engine = CommunityDiscoveryEngine()
        communities = await engine.extract_communities_from_screenshot(
            "https://example.com",
            "Example Page"
        )
        
        assert len(communities) == 2
        assert communities[0].name == "Sunset Ridge Estates"
        assert communities[0].city == "Aventura"
        assert communities[1].name == "Palm Lakes Village"
        assert communities[1].confidence == 0.8


class TestVisionIntegration:
    @pytest.mark.asyncio
    async def test_discovery_falls_back_to_vision(self, monkeypatch, tmp_path):
        """Discovery should use vision when text extraction returns insufficient content."""
        from modules.community.discovery import CommunityDiscoveryEngine, DiscoveredCommunity
        
        # Mock web_search to return results
        def mock_web_search(*args, **kwargs):
            from modules.community.tools import SearchResult
            return [
                SearchResult(url="https://example.com/communities", title="Community List", snippet="test")
            ]
        
        # Mock read_page to return empty content (simulating JS-heavy page)
        async def mock_read_page(*args, **kwargs):
            return ""
        
        # Mock extract_communities_from_screenshot
        async def mock_extract_from_screenshot(*args, **kwargs):
            return [
                DiscoveredCommunity(
                    name="Vision Community",
                    source_url="https://example.com/communities",
                    source_name="Community List",
                    confidence=0.8
                )
            ]
        
        monkeypatch.setattr("modules.community.discovery.web_search", mock_web_search)
        monkeypatch.setattr("modules.community.discovery.read_page", mock_read_page)
        monkeypatch.setattr("modules.community.discovery.CommunityDiscoveryEngine.extract_communities_from_screenshot", mock_extract_from_screenshot)
        
        engine = CommunityDiscoveryEngine(seed_queries=["test query"], state_path=tmp_path / "state.json")
        result = await engine.discover()
        
        assert len(result.communities) == 1
        assert result.communities[0].name == "Vision Community"


# ── database ──────────────────────────────────────────────────────────────────


class TestDatabase:
    def test_create_tables(self, tmp_path):
        from modules.community.database import CommunityDatabase

        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()
        assert db.count() == 0

    def test_upsert_and_load(self, tmp_path):
        from modules.community.database import CommunityDatabase

        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()

        record = CommunityRecord(
            identity=CommunityIdentity(
                name="Test Community",
                slug="test-community",
                city="Miami",
                county_fips="12086",
                is_gated=True,
            )
        )
        slug = db.upsert_record(record, data_source="test")
        assert slug == "test-community"
        assert db.count() == 1

        loaded = db.load_record("test-community")
        assert loaded is not None
        assert loaded.identity.name == "Test Community"
        assert loaded.identity.city == "Miami"

    def test_upsert_with_fees_and_amenities(self, tmp_path):
        from modules.community.database import CommunityDatabase

        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()

        record = CommunityRecord(
            identity=CommunityIdentity(name="Fee Community", slug="fee-community"),
            fees=[
                FeeFact(fee_type="hoa_monthly", amount=450.0, source_url="https://x.example.com"),
                FeeFact(fee_type="cdd_assessment", amount=1200.0, source_url="https://x.example.com"),
            ],
            amenities=[
                AmenityFact(amenity="pool", source_url="https://x.example.com"),
                AmenityFact(amenity="golf", detail="18 holes", source_url="https://x.example.com"),
            ],
        )
        db.upsert_record(record)
        loaded = db.load_record("fee-community")
        assert loaded is not None
        assert len(loaded.fees) == 2
        assert len(loaded.amenities) == 2
        assert loaded.amenities[1].detail == "18 holes"

    def test_upsert_with_demographics(self, tmp_path):
        from modules.community.database import CommunityDatabase

        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()

        record = CommunityRecord(
            identity=CommunityIdentity(name="Demo Community", slug="demo-community"),
            demographics=Demographics(
                median_age=52.0,
                median_household_income=120000.0,
                population=3000,
                source_url="https://census.example.com",
            ),
        )
        db.upsert_record(record)
        loaded = db.load_record("demo-community")
        assert loaded is not None
        assert loaded.demographics is not None
        assert loaded.demographics.median_age == 52.0

    def test_query_by_county(self, tmp_path):
        from modules.community.database import CommunityDatabase

        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()

        for name, county in [("A", "12086"), ("B", "12099"), ("C", "12086")]:
            db.upsert_record(CommunityRecord(
                identity=CommunityIdentity(name=name, slug=slugify(name), county_fips=county),
            ))

        results = db.query_communities(county_fips="12086")
        assert len(results) == 2
        assert {r.identity.name for r in results} == {"A", "C"}

    def test_query_by_amenity(self, tmp_path):
        from modules.community.database import CommunityDatabase

        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()

        db.upsert_record(CommunityRecord(
            identity=CommunityIdentity(name="Golf Community", slug="golf-community"),
            amenities=[AmenityFact(amenity="golf", source_url="https://x.example.com")],
        ))
        db.upsert_record(CommunityRecord(
            identity=CommunityIdentity(name="Pool Community", slug="pool-community"),
            amenities=[AmenityFact(amenity="pool", source_url="https://x.example.com")],
        ))

        results = db.query_communities(has_amenity="golf")
        assert len(results) == 1
        assert results[0].identity.name == "Golf Community"

    def test_summary(self, tmp_path):
        from modules.community.database import CommunityDatabase

        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()

        db.upsert_record(CommunityRecord(
            identity=CommunityIdentity(name="A", slug="a", county_fips="12086", is_gated=True),
            fees=[FeeFact(fee_type="hoa_monthly", amount=100.0, source_url="https://x.example.com")],
        ))
        db.upsert_record(CommunityRecord(
            identity=CommunityIdentity(name="B", slug="b", county_fips="12099", is_gated=False),
        ))

        summary = db.summary()
        assert summary["total_communities"] == 2
        assert summary["gated_count"] == 1
        assert summary["with_fees"] == 1
        assert summary["by_county"]["12086"] == 1
        assert summary["by_county"]["12099"] == 1

    def test_export_csv(self, tmp_path):
        from modules.community.database import CommunityDatabase

        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()

        db.upsert_record(CommunityRecord(
            identity=CommunityIdentity(name="Export Community", slug="export-community", city="Miami"),
            fees=[FeeFact(fee_type="hoa_monthly", amount=300.0, source_url="https://x.example.com")],
            amenities=[AmenityFact(amenity="pool", source_url="https://x.example.com")],
        ))

        csv_path = tmp_path / "export.csv"
        db.export_csv(csv_path)
        assert csv_path.exists()
        content = csv_path.read_text(encoding="utf-8")
        assert "Export Community" in content
        assert "pool" in content

    def test_import_from_store(self, tmp_path):
        from modules.community.database import CommunityDatabase
        from modules.community.store import CommunityStore

        store_root = tmp_path / "store"
        store_root.mkdir()
        store = CommunityStore(store_root)
        record = store.upsert_identity({"name": "Store Community", "city": "Boca Raton"})
        store.save_community(record)

        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()
        count = db.import_from_store(store_root)
        assert count == 1
        loaded = db.load_record("store-community")
        assert loaded is not None
        assert loaded.identity.name == "Store Community"


# ── AI condenser ──────────────────────────────────────────────────────────────


CONDENSED_ITEM = {
    "name": "AI Community",
    "city": "Miami",
    "county": "Miami-Dade",
    "is_gated": True,
    "confidence": 0.8,
    "overview": "A nice gated community",
    "fees": [
        {
            "fee_type": "hoa_monthly",
            "amount": 500.0,
            "source_url": "ai:condensed",
            "confidence": 0.7,
        }
    ],
    "amenities": [
        {"amenity": "pool", "source_url": "ai:condensed", "confidence": 0.8},
    ],
    "demographics": None,
    "proximity": [],
    "hoa_name": None,
}

CONDENSED_BATCH = {
    "communities": [CONDENSED_ITEM],
    "query_description": "test",
    "notes": ["test batch"],
}


class TestCondensedModels:
    def test_condensed_community_item_valid(self):
        from modules.community.models import CondensedCommunityItem

        item = CondensedCommunityItem(
            name="Test Community",
            city="Miami",
            confidence=0.8,
        )
        assert item.name == "Test Community"
        assert item.is_gated is True

    def test_condensed_community_item_to_record(self):
        from modules.community.models import CondensedCommunityItem

        item = CondensedCommunityItem(
            name="Test Community",
            city="Miami",
            fees=[FeeFact(fee_type="hoa_monthly", amount=300.0, source_url="ai:condensed")],
            amenities=[AmenityFact(amenity="pool", source_url="ai:condensed")],
        )
        record = item.to_record()
        assert record.identity.slug == "test-community"
        assert len(record.fees) == 1
        assert len(record.amenities) == 1

    def test_condensed_batch_valid(self):
        from modules.community.models import CondensedCommunityBatch, CondensedCommunityItem

        batch = CondensedCommunityBatch(
            communities=[CondensedCommunityItem(name="AA"), CondensedCommunityItem(name="BB")],
            query_description="test",
        )
        assert len(batch.communities) == 2

    def test_ai_source_url_accepted(self):
        fee = FeeFact(fee_type="hoa_monthly", amount=100.0, source_url="ai:condensed")
        assert fee.source_url == "ai:condensed"


class TestAICondenser:
    def test_init_defaults(self):
        from modules.community.ai_condenser import AICondenser

        condenser = AICondenser()
        assert condenser.batch_size == 20

    @pytest.mark.asyncio
    async def test_discover_with_test_model(self, tmp_path):
        from modules.community.ai_condenser import AICondenser
        from modules.community.database import CommunityDatabase

        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()

        condenser = AICondenser(
            model=TestModel(custom_output_args=CONDENSED_BATCH),
            database=db,
            store=CommunityStore(tmp_path / "store"),
        )
        items = await condenser.discover_communities(limit=10)
        assert len(items) == 1
        assert items[0].name == "AI Community"

    @pytest.mark.asyncio
    async def test_enrich_with_test_model(self, tmp_path):
        from modules.community.ai_condenser import AICondenser
        from modules.community.database import CommunityDatabase

        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()

        condenser = AICondenser(
            model=TestModel(custom_output_args=CONDENSED_ITEM),
            database=db,
            store=CommunityStore(tmp_path / "store"),
        )
        item = await condenser.enrich_community("AI Community")
        assert item.name == "AI Community"
        assert len(item.fees) == 1

    @pytest.mark.asyncio
    async def test_run_discover_stores_in_db(self, tmp_path):
        from modules.community.ai_condenser import AICondenser
        from modules.community.database import CommunityDatabase

        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()

        condenser = AICondenser(
            model=TestModel(custom_output_args=CONDENSED_BATCH),
            database=db,
            store=CommunityStore(tmp_path / "store"),
        )
        results = await condenser.run_discover(limit=10)
        assert len(results) == 1
        assert results[0].community == "AI Community"
        assert db.count() == 1

        loaded = db.load_record("ai-community")
        assert loaded is not None
        assert loaded.identity.name == "AI Community"
        assert len(loaded.fees) == 1

    @pytest.mark.asyncio
    async def test_run_enrich_from_target_list(self, tmp_path):
        from modules.community.ai_condenser import AICondenser
        from modules.community.database import CommunityDatabase

        store_dir = tmp_path / "store"
        store_dir.mkdir()
        store = CommunityStore(store_dir)
        (store_dir / "target_list.json").write_text(
            json.dumps({"communities": [{"name": "Pelican Bay", "city": "Key Biscayne"}]}),
            encoding="utf-8",
        )

        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()

        condenser = AICondenser(
            model=TestModel(custom_output_args=CONDENSED_ITEM),
            database=db,
            store=store,
        )
        results = await condenser.run_enrich(limit=1)
        assert len(results) == 1
        assert results[0].community == "Pelican Bay"

    @pytest.mark.asyncio
    async def test_summary_after_condense(self, tmp_path):
        from modules.community.ai_condenser import AICondenser
        from modules.community.database import CommunityDatabase

        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()

        condenser = AICondenser(
            model=TestModel(custom_output_args=CONDENSED_BATCH),
            database=db,
            store=CommunityStore(tmp_path / "store"),
        )
        await condenser.run_discover(limit=10)
        summary = condenser.get_summary()
        assert summary["total_communities"] == 1
        assert summary["data_sources"]["ai_condenser"] == 1


# ── Browser Condenser ─────────────────────────────────────────────────────────


class TestBrowserCondenserModels:
    def test_browser_source_url(self):
        from modules.community.models import BROWSER_SOURCE_URL
        assert BROWSER_SOURCE_URL == "browser:google"

    def test_gap_analysis_model(self):
        from modules.community.models import GapAnalysis
        
        gap = GapAnalysis(
            total_communities=10,
            communities_missing_fees=["Community A", "Community B"],
            communities_missing_amenities=["Community C"],
            priority_gap="fees",
            priority_target="Community A",
            has_any_gaps=True,
        )
        assert gap.total_communities == 10
        assert len(gap.communities_missing_fees) == 2
        assert gap.priority_gap == "fees"

    def test_next_query_model(self):
        from modules.community.models import NextQuery
        
        query = NextQuery(
            query="gated communities in Miami-Dade County",
            target_type="discover",
            expected_result="List of communities with basic info",
            reasoning="Need to expand geographic coverage",
        )
        assert query.target_type == "discover"
        assert "Miami-Dade" in query.query

    def test_browser_condenser_state_model(self):
        from modules.community.models import BrowserCondenserState
        
        state = BrowserCondenserState(
            queries_executed=["query1", "query2"],
            communities_discovered=["Community A"],
            total_iterations=2,
        )
        assert len(state.queries_executed) == 2
        assert state.total_iterations == 2

    def test_google_search_result_model(self):
        from modules.community.models import GoogleSearchResult
        
        result = GoogleSearchResult(
            title="Test Result",
            url="https://example.com",
            snippet="Test snippet",
            position=1,
        )
        assert result.position == 1
        assert result.url == "https://example.com"

    def test_florida_counties(self):
        from modules.community.models import FLORIDA_COUNTIES
        
        assert len(FLORIDA_COUNTIES) == 67
        assert "Miami-Dade" in FLORIDA_COUNTIES
        assert "Broward" in FLORIDA_COUNTIES
        assert "Palm Beach" in FLORIDA_COUNTIES


class TestBrowserCondenserConfig:
    def test_default_config(self):
        from modules.community.browser_condenser import BrowserCondenserConfig
        
        config = BrowserCondenserConfig()
        assert config.max_iterations == 20
        assert config.max_results_per_query == 10
        assert config.headless is False

    def test_custom_config(self):
        from modules.community.browser_condenser import BrowserCondenserConfig
        
        config = BrowserCondenserConfig(
            max_iterations=50,
            headless=True,
            chrome_profile_path="/path/to/profile",
        )
        assert config.max_iterations == 50
        assert config.headless is True
        assert config.chrome_profile_path == "/path/to/profile"


class TestBrowserCondenser:
    def test_init(self, tmp_path):
        from modules.community.browser_condenser import BrowserCondenser
        from modules.community.database import CommunityDatabase
        
        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()
        
        condenser = BrowserCondenser(database=db, state_path=tmp_path / "state.json")
        assert condenser.database is not None
        assert condenser.state_path == tmp_path / "state.json"

    def test_save_and_load_state(self, tmp_path):
        from modules.community.browser_condenser import BrowserCondenser
        from modules.community.database import CommunityDatabase
        
        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()
        
        state_path = tmp_path / "state.json"
        condenser = BrowserCondenser(database=db, state_path=state_path)
        
        condenser.state.queries_executed.append("test query")
        condenser.state.total_iterations = 5
        condenser._save_state()
        
        assert state_path.exists()
        
        # Load in new condenser
        condenser2 = BrowserCondenser(database=db, state_path=state_path)
        assert condenser2.state.total_iterations == 5
        assert "test query" in condenser2.state.queries_executed

    @pytest.mark.asyncio
    async def test_analyze_gaps_empty_db(self, tmp_path):
        from modules.community.browser_condenser import BrowserCondenser
        from modules.community.database import CommunityDatabase
        from pydantic_ai.models.test import TestModel
        
        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()
        
        gap_analysis_output = {
            "total_communities": 0,
            "communities_missing_fees": [],
            "communities_missing_amenities": [],
            "communities_missing_demographics": [],
            "communities_missing_proximity": [],
            "counties_with_data": [],
            "counties_missing_data": ["Miami-Dade", "Broward"],
            "priority_gap": "geographic",
            "priority_target": "Miami-Dade",
            "reasoning": "No communities in database",
            "has_any_gaps": True,
        }
        
        condenser = BrowserCondenser(
            database=db,
            state_path=tmp_path / "state.json",
        )
        condenser._model = TestModel(custom_output_args=gap_analysis_output)
        
        gaps = await condenser.analyze_gaps()
        assert gaps.priority_gap == "geographic"
        assert gaps.priority_target == "Miami-Dade"

    @pytest.mark.asyncio
    async def test_generate_query(self, tmp_path):
        from modules.community.browser_condenser import BrowserCondenser
        from modules.community.database import CommunityDatabase
        from modules.community.models import GapAnalysis
        from pydantic_ai.models.test import TestModel
        
        db = CommunityDatabase(tmp_path / "test.db")
        db.create_tables()
        
        query_output = {
            "query": "gated communities in Miami-Dade County Florida",
            "reasoning": "Need to discover communities in Miami-Dade",
            "target_type": "discover",
            "expected_result": "List of communities",
        }
        
        condenser = BrowserCondenser(
            database=db,
            state_path=tmp_path / "state.json",
        )
        condenser._model = TestModel(custom_output_args=query_output)
        
        gap = GapAnalysis(
            priority_gap="geographic",
            priority_target="Miami-Dade",
            has_any_gaps=True,
        )
        
        query = await condenser.generate_query(gap)
        assert "Miami-Dade" in query.query
        assert query.target_type == "discover"

