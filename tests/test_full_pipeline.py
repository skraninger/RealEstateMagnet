"""Tests for the full pipeline module."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.community.full_pipeline import (
    ALL_CONDENSERS,
    CondenserStepResult,
    CommunityInfo,
    FullPipeline,
    PipelineState,
    PipelineStats,
    _discover_all_communities,
    _ensure_community_record,
    _save_condensed_item,
    slugify,
)
from modules.community.database import CommunityDatabase
from modules.community.models import (
    AmenityFact,
    CommunityFacts,
    CommunityIdentity,
    CommunityRecord,
    CondensedCommunityItem,
    Demographics,
    FeeFact,
    ProximityMetric,
)
from modules.community.store import CommunityStore


# ── State models ─────────────────────────────────────────────────────────────


class TestPipelineState:
    """Tests for PipelineState model."""

    def test_empty_state(self) -> None:
        state = PipelineState()
        assert state.version == 1
        assert state.communities == []
        assert state.results == {}
        assert state.stats.total_runs == 0
        assert state.condensers == list(ALL_CONDENSERS)

    def test_save_and_load(self, tmp_path: Path) -> None:
        state = PipelineState()
        state.communities = [
            CommunityInfo(name="Test Community", slug="test-community", city="Miami"),
        ]
        state.results["test-community"] = {
            "ai": CondenserStepResult(status="done", fees=2, amenities=5),
            "web": CondenserStepResult(status="pending"),
        }
        state.stats.total_runs = 1
        state.stats.successful = 1

        path = tmp_path / "pipeline_state.json"
        state.save(path)

        assert path.exists()
        loaded = PipelineState.load(path)
        assert len(loaded.communities) == 1
        assert loaded.communities[0].name == "Test Community"
        assert loaded.results["test-community"]["ai"].status == "done"
        assert loaded.results["test-community"]["ai"].fees == 2
        assert loaded.results["test-community"]["web"].status == "pending"
        assert loaded.stats.total_runs == 1

    def test_load_nonexistent(self, tmp_path: Path) -> None:
        path = tmp_path / "nonexistent.json"
        state = PipelineState.load(path)
        assert state.version == 1
        assert state.communities == []

    def test_pending_work(self) -> None:
        state = PipelineState()
        state.communities = [
            CommunityInfo(name="A", slug="a"),
            CommunityInfo(name="B", slug="b"),
        ]
        state.condensers = ["ai", "web", "research"]
        state.results = {
            "a": {
                "ai": CondenserStepResult(status="done"),
                "web": CondenserStepResult(status="pending"),
                "research": CondenserStepResult(status="error"),
            },
            "b": {
                "ai": CondenserStepResult(status="done"),
                "web": CondenserStepResult(status="done"),
                "research": CondenserStepResult(status="pending"),
            },
        }
        work = state.pending_work()
        assert ("a", "web") in work
        assert ("a", "research") in work
        assert ("b", "research") in work
        assert ("a", "ai") not in work
        assert ("b", "ai") not in work
        assert ("b", "web") not in work
        assert len(work) == 3

    def test_community_progress(self) -> None:
        state = PipelineState()
        state.condensers = ["ai", "web"]
        state.results = {
            "test": {
                "ai": CondenserStepResult(status="done"),
                "web": CondenserStepResult(status="pending"),
            },
        }
        progress = state.community_progress("test")
        assert progress["ai"] == "done"
        assert progress["web"] == "pending"


# ── Community info ───────────────────────────────────────────────────────────


class TestCommunityInfo:
    """Tests for CommunityInfo model."""

    def test_basic_info(self) -> None:
        info = CommunityInfo(name="Test", slug="test", city="Miami", source="target_list")
        assert info.name == "Test"
        assert info.slug == "test"
        assert info.city == "Miami"
        assert info.source == "target_list"

    def test_optional_fields(self) -> None:
        info = CommunityInfo(name="Test", slug="test")
        assert info.city is None
        assert info.county is None
        assert info.source == "unknown"


# ── Condenser step result ────────────────────────────────────────────────────


class TestCondenserStepResult:
    """Tests for CondenserStepResult model."""

    def test_default_status(self) -> None:
        result = CondenserStepResult()
        assert result.status == "pending"
        assert result.elapsed == 0.0
        assert result.fees == 0
        assert result.amenities == 0
        assert result.errors == []

    def test_done_status(self) -> None:
        result = CondenserStepResult(
            status="done",
            elapsed=3.2,
            fees=2,
            amenities=5,
            proximity=3,
            demographics_present=True,
        )
        assert result.status == "done"
        assert result.elapsed == 3.2
        assert result.fees == 2
        assert result.demographics_present is True

    def test_error_status(self) -> None:
        result = CondenserStepResult(status="error", errors=["Something failed"])
        assert result.status == "error"
        assert "Something failed" in result.errors


# ── Discovery ────────────────────────────────────────────────────────────────


class TestDiscovery:
    """Tests for community discovery from multiple sources."""

    def test_discover_from_target_list(self, tmp_path: Path) -> None:
        store = CommunityStore(tmp_path / "communities")
        store.root.mkdir(parents=True, exist_ok=True)
        store.target_list_path.write_text(
            json.dumps({
                "communities": [
                    {"name": "Community A", "city": "Miami"},
                    {"name": "Community B", "city": "Naples"},
                ]
            }),
            encoding="utf-8",
        )
        database = MagicMock()
        database.list_records.return_value = []
        # Use a non-existent discovery state path to avoid reading real data
        discovery_state_path = tmp_path / "discovery_state.json"

        communities = _discover_all_communities(store, database, discovery_state_path)
        assert len(communities) == 2
        assert communities[0].name == "Community A"
        assert communities[0].source == "target_list"
        assert communities[1].name == "Community B"

    def test_discover_from_database(self, tmp_path: Path) -> None:
        store = CommunityStore(tmp_path / "communities")
        database = MagicMock()
        record = MagicMock()
        record.identity.name = "DB Community"
        record.identity.slug = "db-community"
        record.identity.city = "Fort Lauderdale"
        record.identity.county_fips = None
        database.list_records.return_value = [record]
        # Use a non-existent discovery state path to avoid reading real data
        discovery_state_path = tmp_path / "discovery_state.json"

        communities = _discover_all_communities(store, database, discovery_state_path)
        assert len(communities) == 1
        assert communities[0].name == "DB Community"
        assert communities[0].source == "database"

    def test_discover_deduplicates_by_slug(self, tmp_path: Path) -> None:
        store = CommunityStore(tmp_path / "communities")
        store.root.mkdir(parents=True, exist_ok=True)
        store.target_list_path.write_text(
            json.dumps({
                "communities": [
                    {"name": "Pelican Bay", "city": "Naples"},
                ]
            }),
            encoding="utf-8",
        )
        database = MagicMock()
        record = MagicMock()
        record.identity.name = "Pelican Bay"
        record.identity.slug = "pelican-bay"
        record.identity.city = "Naples"
        record.identity.county_fips = None
        database.list_records.return_value = [record]
        # Use a non-existent discovery state path to avoid reading real data
        discovery_state_path = tmp_path / "discovery_state.json"

        communities = _discover_all_communities(store, database, discovery_state_path)
        # Should deduplicate - only one "Pelican Bay"
        assert len(communities) == 1
        assert communities[0].name == "Pelican Bay"
        # Target list takes precedence
        assert communities[0].source == "target_list"

    def test_discover_from_discovery_state(self, tmp_path: Path) -> None:
        store = CommunityStore(tmp_path / "communities")
        store.root.mkdir(parents=True, exist_ok=True)
        database = MagicMock()
        database.list_records.return_value = []
        
        # Create a discovery state file
        discovery_state_path = tmp_path / "discovery_state.json"
        discovery_state_path.write_text(
            json.dumps({
                "discovered_communities": [
                    {"name": "Discovery Community", "city": "Miami", "county": "Miami-Dade"},
                ]
            }),
            encoding="utf-8",
        )

        communities = _discover_all_communities(store, database, discovery_state_path)
        assert len(communities) == 1
        assert communities[0].name == "Discovery Community"
        assert communities[0].source == "discovery"


# ── Save condensed item ──────────────────────────────────────────────────────


class TestSaveCondensedItem:
    """Tests for saving condensed community items."""

    def test_save_item(self, tmp_path: Path) -> None:
        store = CommunityStore(tmp_path / "communities")
        database = MagicMock()
        database.upsert_record = MagicMock()

        item = CondensedCommunityItem(
            name="Test Community",
            city="Miami",
            fees=[
                FeeFact(
                    fee_type="hoa_monthly",
                    amount=150.0,
                    source_url="ai:condensed",
                    retrieved_at="2026-01-01T00:00:00Z",
                )
            ],
            amenities=[
                AmenityFact(
                    amenity="pool",
                    source_url="ai:condensed",
                    retrieved_at="2026-01-01T00:00:00Z",
                )
            ],
        )

        slug = _save_condensed_item(item, store, database, "ai_condenser")
        assert slug == "test-community"
        database.upsert_record.assert_called_once()


# ── Ensure community record ──────────────────────────────────────────────────


class TestEnsureCommunityRecord:
    """Tests for ensuring community records exist."""

    def test_create_new_record(self, tmp_path: Path) -> None:
        store = CommunityStore(tmp_path / "communities")
        database = MagicMock()
        database.upsert_record = MagicMock()

        info = CommunityInfo(name="New Community", slug="new-community", city="Miami")
        record = _ensure_community_record(info, store, database)

        assert record.identity.name == "New Community"
        assert record.identity.slug == "new-community"
        assert record.identity.city == "Miami"
        store.save_community(record)
        assert (store.communities_dir / "new-community.json").exists()

    def test_load_existing_record(self, tmp_path: Path) -> None:
        store = CommunityStore(tmp_path / "communities")
        database = MagicMock()

        # Create existing record
        existing = CommunityRecord(
            id="12345678-1234-1234-1234-123456789abc",
            identity=CommunityIdentity(name="Existing", slug="existing"),
            fees=[],
            amenities=[],
            proximity=[],
        )
        store.save_community(existing)

        info = CommunityInfo(name="Existing", slug="existing")
        record = _ensure_community_record(info, store, database)

        assert record.identity.name == "Existing"
        assert record.identity.slug == "existing"


# ── Full pipeline ────────────────────────────────────────────────────────────


class TestFullPipeline:
    """Tests for the FullPipeline class."""

    def test_init_state(self, tmp_path: Path) -> None:
        store = CommunityStore(tmp_path / "communities")
        store.root.mkdir(parents=True, exist_ok=True)
        store.target_list_path.write_text(
            json.dumps({
                "communities": [
                    {"name": "Community A", "city": "Miami"},
                    {"name": "Community B", "city": "Naples"},
                ]
            }),
            encoding="utf-8",
        )
        database = MagicMock()
        database.list_records.return_value = []
        # Use non-existent discovery state path
        discovery_state_path = tmp_path / "discovery_state.json"

        state_path = tmp_path / "pipeline_state.json"
        pipeline = FullPipeline(
            state_path=state_path,
            store=store,
            database=database,
            discovery_state_path=discovery_state_path,
        )

        state = pipeline.init_state()
        assert len(state.communities) == 2
        assert state.communities[0].name == "Community A"
        assert state.communities[1].name == "Community B"
        assert state_path.exists()

    def test_init_state_with_reset(self, tmp_path: Path) -> None:
        store = CommunityStore(tmp_path / "communities")
        store.root.mkdir(parents=True, exist_ok=True)
        store.target_list_path.write_text(
            json.dumps({"communities": []}),
            encoding="utf-8",
        )
        database = MagicMock()
        database.list_records.return_value = []
        # Use non-existent discovery state path
        discovery_state_path = tmp_path / "discovery_state.json"

        state_path = tmp_path / "pipeline_state.json"

        # Create initial state
        initial_state = PipelineState()
        initial_state.communities = [
            CommunityInfo(name="Old", slug="old"),
        ]
        initial_state.save(state_path)

        # Reset should discard old state
        pipeline = FullPipeline(
            state_path=state_path,
            store=store,
            database=database,
            discovery_state_path=discovery_state_path,
        )
        state = pipeline.init_state(reset=True)
        assert len(state.communities) == 0

    def test_condenser_order(self) -> None:
        """Verify condensers run in the correct order."""
        assert ALL_CONDENSERS == ["ai", "web", "research", "browser", "gemini"]

    @pytest.mark.asyncio
    async def test_run_with_no_work(self, tmp_path: Path) -> None:
        """Pipeline should complete immediately if no work to do."""
        store = CommunityStore(tmp_path / "communities")
        store.root.mkdir(parents=True, exist_ok=True)
        store.target_list_path.write_text(
            json.dumps({"communities": []}),
            encoding="utf-8",
        )
        database = MagicMock()
        database.list_records.return_value = []
        database.summary.return_value = {
            "total_communities": 0,
            "gated_count": 0,
            "with_fees": 0,
            "with_amenities": 0,
            "with_demographics": 0,
        }
        # Use non-existent discovery state path
        discovery_state_path = tmp_path / "discovery_state.json"

        state_path = tmp_path / "pipeline_state.json"
        pipeline = FullPipeline(
            state_path=state_path,
            store=store,
            database=database,
            discovery_state_path=discovery_state_path,
        )

        state = await pipeline.run()
        assert len(state.communities) == 0

    @pytest.mark.asyncio
    async def test_run_with_community_filter(self, tmp_path: Path) -> None:
        """Pipeline should only process matching communities."""
        store = CommunityStore(tmp_path / "communities")
        store.root.mkdir(parents=True, exist_ok=True)
        store.target_list_path.write_text(
            json.dumps({
                "communities": [
                    {"name": "Pelican Bay", "city": "Naples"},
                    {"name": "Pointe Royal", "city": "Boca Raton"},
                ]
            }),
            encoding="utf-8",
        )
        database = MagicMock()
        database.list_records.return_value = []
        database.summary.return_value = {
            "total_communities": 1,
            "gated_count": 1,
            "with_fees": 0,
            "with_amenities": 0,
            "with_demographics": 0,
        }
        discovery_state_path = tmp_path / "discovery_state.json"

        state_path = tmp_path / "pipeline_state.json"
        pipeline = FullPipeline(
            state_path=state_path,
            store=store,
            database=database,
            condensers=["ai"],
            discovery_state_path=discovery_state_path,
        )

        mock_record = MagicMock()
        mock_record.identity.name = "Pelican Bay"
        mock_record.identity.slug = "pelican-bay"

        async def mock_ai_runner(*args, **kwargs):
            return CondenserStepResult(status="done", fees=1, amenities=2, elapsed=0.1)

        # Patch the runners dict and _ensure_community_record
        with patch.dict(
            "modules.community.full_pipeline._CONDENSER_RUNNERS",
            {"ai": mock_ai_runner},
        ), patch(
            "modules.community.full_pipeline._ensure_community_record",
            return_value=mock_record,
        ):
            state = await pipeline.run(community="Pelican")

        pelican_slug = slugify("Pelican Bay")
        pointe_slug = slugify("Pointe Royal")
        assert pelican_slug in state.results
        assert state.results[pelican_slug]["ai"].status == "done"
        assert state.results.get(pointe_slug, {}).get("ai", CondenserStepResult()).status in (
            "pending",
            None,
        )


# ── Integration tests ────────────────────────────────────────────────────────


class TestPipelineIntegration:
    """Integration tests for the pipeline with mocked condensers."""

    @pytest.mark.asyncio
    async def test_full_cycle(self, tmp_path: Path) -> None:
        """Test a complete pipeline cycle with mocked condensers."""
        store = CommunityStore(tmp_path / "communities")
        store.root.mkdir(parents=True, exist_ok=True)
        store.target_list_path.write_text(
            json.dumps({
                "communities": [
                    {"name": "Test Community", "city": "Miami"},
                ]
            }),
            encoding="utf-8",
        )
        database = MagicMock()
        database.list_records.return_value = []
        database.import_from_store = MagicMock()
        database.summary.return_value = {
            "total_communities": 1,
            "gated_count": 1,
            "with_fees": 1,
            "with_amenities": 2,
            "with_demographics": 0,
        }
        discovery_state_path = tmp_path / "discovery_state.json"

        state_path = tmp_path / "pipeline_state.json"
        pipeline = FullPipeline(
            state_path=state_path,
            store=store,
            database=database,
            condensers=["ai", "web"],
            discovery_state_path=discovery_state_path,
        )

        mock_record = MagicMock()
        mock_record.identity.name = "Test Community"
        mock_record.identity.slug = "test-community"

        async def mock_condenser(*args, **kwargs):
            return CondenserStepResult(status="done", fees=2, amenities=5, elapsed=0.1)

        with patch.dict(
            "modules.community.full_pipeline._CONDENSER_RUNNERS",
            {"ai": mock_condenser, "web": mock_condenser},
        ), patch(
            "modules.community.full_pipeline._ensure_community_record",
            return_value=mock_record,
        ):
            state = await pipeline.run()

        # Verify state
        assert len(state.communities) == 1
        slug = slugify("Test Community")
        assert state.results[slug]["ai"].status == "done"
        assert state.results[slug]["web"].status == "done"
        assert state.stats.total_runs == 2
        assert state.stats.successful == 2

        # Verify state file was saved
        assert state_path.exists()
        loaded = PipelineState.load(state_path)
        assert loaded.stats.total_runs == 2

    @pytest.mark.asyncio
    async def test_resumability(self, tmp_path: Path) -> None:
        """Test that pipeline can resume from saved state."""
        store = CommunityStore(tmp_path / "communities")
        store.root.mkdir(parents=True, exist_ok=True)
        store.target_list_path.write_text(
            json.dumps({
                "communities": [
                    {"name": "Community A", "city": "Miami"},
                    {"name": "Community B", "city": "Naples"},
                ]
            }),
            encoding="utf-8",
        )
        database = MagicMock()
        database.list_records.return_value = []
        database.import_from_store = MagicMock()
        database.summary.return_value = {
            "total_communities": 2,
            "gated_count": 2,
            "with_fees": 2,
            "with_amenities": 4,
            "with_demographics": 0,
        }
        discovery_state_path = tmp_path / "discovery_state.json"

        state_path = tmp_path / "pipeline_state.json"

        # First run: process only AI condenser
        pipeline1 = FullPipeline(
            state_path=state_path,
            store=store,
            database=database,
            condensers=["ai", "web"],
            discovery_state_path=discovery_state_path,
        )

        async def mock_condenser(*args, **kwargs):
            return CondenserStepResult(status="done", fees=1, amenities=2, elapsed=0.1)

        mock_record = MagicMock()
        mock_record.identity.name = "Community A"
        mock_record.identity.slug = "community-a"

        with patch.dict(
            "modules.community.full_pipeline._CONDENSER_RUNNERS",
            {"ai": mock_condenser, "web": mock_condenser},
        ), patch(
            "modules.community.full_pipeline._ensure_community_record",
            return_value=mock_record,
        ):
            state1 = await pipeline1.run()

        # Verify first run completed
        assert state1.stats.total_runs == 4  # 2 communities × 2 condensers
        assert state1.stats.successful == 4

        # Second run: should detect no work to do
        pipeline2 = FullPipeline(
            state_path=state_path,
            store=store,
            database=database,
            condensers=["ai", "web"],
            discovery_state_path=discovery_state_path,
        )

        state2 = await pipeline2.run()
        # Should still show 4 runs from first run, no new runs
        assert state2.stats.total_runs == 4


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
