"""Tests for URL tracking and vision browser agent functionality.

Tests cover:
- BrowserAction and BrowserExtractResult model validation
- SourceURLRow table CRUD operations
- URLTracker registration and review queue
- Vision browser agent with mocked LLM
- Failure classification logic
"""

import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from modules.community.models import BrowserAction, BrowserExtractResult
from modules.community.database import CommunityDatabase, SourceURLRow
from modules.community.url_tracker import URLTracker


class TestBrowserAction:
    """Test BrowserAction model validation."""

    def test_click_action_valid(self):
        """Test valid click action with coordinates."""
        action = BrowserAction(
            action="click",
            x=100,
            y=200,
            reasoning="Clicking on submit button"
        )
        assert action.action == "click"
        assert action.x == 100
        assert action.y == 200
        assert action.reasoning == "Clicking on submit button"

    def test_captcha_click_action_valid(self):
        """Test valid CAPTCHA click action."""
        action = BrowserAction(
            action="captcha_click",
            x=50,
            y=100,
            reasoning="Clicking CAPTCHA checkbox"
        )
        assert action.action == "captcha_click"
        assert action.x == 50
        assert action.y == 100

    def test_type_action_with_selector(self):
        """Test type action with CSS selector."""
        action = BrowserAction(
            action="type",
            selector="#search-input",
            text="Miami gated communities",
            reasoning="Typing search query"
        )
        assert action.action == "type"
        assert action.selector == "#search-input"
        assert action.text == "Miami gated communities"

    def test_type_action_with_coordinates(self):
        """Test type action with coordinates."""
        action = BrowserAction(
            action="type",
            x=100,
            y=200,
            text="Test input",
            reasoning="Clicking and typing"
        )
        assert action.action == "type"
        assert action.x == 100
        assert action.y == 200
        assert action.text == "Test input"

    def test_scroll_action_valid(self):
        """Test valid scroll action."""
        action = BrowserAction(
            action="scroll",
            direction="down",
            reasoning="Scrolling to see more content"
        )
        assert action.action == "scroll"
        assert action.direction == "down"

    def test_wait_action_valid(self):
        """Test valid wait action."""
        action = BrowserAction(
            action="wait",
            seconds=5.0,
            reasoning="Waiting for page to load"
        )
        assert action.action == "wait"
        assert action.seconds == 5.0

    def test_done_action_valid(self):
        """Test valid done action with content."""
        action = BrowserAction(
            action="done",
            summary="Page contains community information",
            page_content="Full text content here...",
            reasoning="Successfully extracted content"
        )
        assert action.action == "done"
        assert action.summary == "Page contains community information"
        assert action.page_content == "Full text content here..."

    def test_give_up_action_valid(self):
        """Test valid give_up action."""
        action = BrowserAction(
            action="give_up",
            error_description="CAPTCHA could not be solved after 3 attempts",
            reasoning="Giving up due to unsolvable CAPTCHA"
        )
        assert action.action == "give_up"
        assert action.error_description == "CAPTCHA could not be solved after 3 attempts"

    def test_action_requires_reasoning(self):
        """Test that reasoning field has default empty string."""
        action = BrowserAction(action="click", x=100, y=200)
        assert action.reasoning == ""

    def test_invalid_action_type(self):
        """Test that invalid action type raises validation error."""
        with pytest.raises(ValueError):
            BrowserAction(action="invalid_action")


class TestBrowserExtractResult:
    """Test BrowserExtractResult model validation."""

    def test_successful_result(self):
        """Test successful extraction result."""
        result = BrowserExtractResult(
            url="https://example.com",
            success=True,
            status="printed",
            page_title="Example Page",
            page_content="Full text content",
            content_length=1000,
            pdf_path="/path/to/file.pdf",
            screenshot_path="/path/to/screenshot.png",
            http_status=200,
            captcha_detected=False,
            captcha_solved=False,
            steps_taken=3,
            elapsed_seconds=15.5
        )
        assert result.success is True
        assert result.status == "printed"
        assert result.content_length == 1000

    def test_failure_result_4xx(self):
        """Test 4xx failure result."""
        result = BrowserExtractResult(
            url="https://example.com/blocked",
            success=False,
            status="failed_4xx",
            http_status=403,
            failure_category="bot_detection",
            failure_detail="Access denied - automated access not allowed",
            is_retryable=False,
            steps_taken=1,
            elapsed_seconds=5.0
        )
        assert result.success is False
        assert result.status == "failed_4xx"
        assert result.failure_category == "bot_detection"
        assert result.is_retryable is False

    def test_captcha_result(self):
        """Test result with CAPTCHA detection."""
        result = BrowserExtractResult(
            url="https://example.com",
            success=True,
            status="printed",
            captcha_detected=True,
            captcha_solved=True,
            steps_taken=5,
            elapsed_seconds=30.0
        )
        assert result.captcha_detected is True
        assert result.captcha_solved is True

    def test_timeout_result(self):
        """Test timeout failure result."""
        result = BrowserExtractResult(
            url="https://example.com",
            success=False,
            status="failed_timeout",
            failure_category="timeout",
            failure_detail="Page load timeout after 30 seconds",
            is_retryable=True,
            steps_taken=0,
            elapsed_seconds=30.0
        )
        assert result.status == "failed_timeout"
        assert result.is_retryable is True

    def test_invalid_status(self):
        """Test that invalid status raises validation error."""
        with pytest.raises(ValueError):
            BrowserExtractResult(
                url="https://example.com",
                success=False,
                status="invalid_status"
            )

    def test_invalid_failure_category(self):
        """Test that invalid failure category raises validation error."""
        with pytest.raises(ValueError):
            BrowserExtractResult(
                url="https://example.com",
                success=False,
                status="failed_4xx",
                failure_category="invalid_category"
            )


class TestSourceURLRow:
    """Test SourceURLRow database model."""

    def test_create_source_url_row(self):
        """Test creating a SourceURLRow."""
        row = SourceURLRow(
            url="https://example.com",
            domain="example.com",
            status="pending",
            discovered_by="test"
        )
        assert row.url == "https://example.com"
        assert row.domain == "example.com"
        assert row.status == "pending"
        assert row.discovered_by == "test"
        # Note: reviewed is None before persistence (SQLAlchemy default applies at DB level)
        assert row.reviewed is None or row.reviewed is False
        assert row.captcha_detected is None or row.captcha_detected is False
        assert row.captcha_solved is None or row.captcha_solved is False

    def test_source_url_row_with_failure(self):
        """Test SourceURLRow with failure information."""
        row = SourceURLRow(
            url="https://example.com/blocked",
            domain="example.com",
            status="failed_4xx",
            discovered_by="test",
            http_status=403,
            failure_category="bot_detection",
            failure_detail="Access denied",
            is_retryable=False,
            reviewed=False
        )
        assert row.status == "failed_4xx"
        assert row.http_status == 403
        assert row.failure_category == "bot_detection"
        assert row.reviewed is False

    def test_source_url_row_defaults(self):
        """Test SourceURLRow default values."""
        row = SourceURLRow(
            url="https://example.com",
            domain="example.com",
            status="pending",
            discovered_by="test"
        )
        # Note: defaults apply at DB level, not Python object level before persistence
        assert row.reviewed in (None, False)
        assert row.captcha_detected in (None, False)
        assert row.captcha_solved in (None, False)
        assert row.retry_count in (None, 0)
        assert row.steps_taken in (None, 0)


class TestURLTracker:
    """Test URLTracker functionality."""

    @pytest.fixture
    def temp_db(self, tmp_path):
        """Create a temporary database."""
        db_path = tmp_path / "test.db"
        db = CommunityDatabase(db_path=db_path)
        db.create_tables()
        return db

    @pytest.fixture
    def tracker(self, temp_db, tmp_path):
        """Create a URLTracker with temporary database."""
        pages_dir = tmp_path / "research_pages"
        return URLTracker(db=temp_db, pages_dir=pages_dir)

    def test_register_url_new(self, tracker):
        """Test registering a new URL."""
        row = tracker.register_url(
            url="https://example.com/page1",
            discovered_by="test",
            community_slug="test-community"
        )
        assert row.url == "https://example.com/page1"
        assert row.domain == "example.com"
        assert row.status == "pending"
        assert row.discovered_by == "test"
        assert row.community_slug == "test-community"

    def test_register_url_duplicate(self, tracker):
        """Test registering the same URL twice returns existing row."""
        row1 = tracker.register_url(
            url="https://example.com/page1",
            discovered_by="test1"
        )
        row2 = tracker.register_url(
            url="https://example.com/page1",
            discovered_by="test2"
        )
        assert row1.id == row2.id
        assert row2.discovered_by == "test1"  # Should keep first discoverer

    def test_register_urls_batch(self, tracker):
        """Test registering multiple URLs."""
        urls = [
            "https://example.com/page1",
            "https://example.com/page2",
            "https://example.com/page3"
        ]
        rows = tracker.register_urls(
            urls=urls,
            discovered_by="test"
        )
        assert len(rows) == 3
        assert all(row.status == "pending" for row in rows)

    def test_mark_accessing(self, tracker):
        """Test marking URL as accessing."""
        row = tracker.register_url(
            url="https://example.com",
            discovered_by="test"
        )
        tracker.mark_accessing("https://example.com")
        
        with tracker.db.session() as session:
            updated = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com"
            ).first()
            assert updated.status == "accessed"
            assert updated.accessed_at is not None

    def test_mark_captcha_solving(self, tracker):
        """Test marking URL as captcha_solving."""
        row = tracker.register_url(
            url="https://example.com",
            discovered_by="test"
        )
        tracker.mark_captcha_solving("https://example.com")
        
        with tracker.db.session() as session:
            updated = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com"
            ).first()
            assert updated.status == "captcha_solving"
            assert updated.captcha_detected is True

    def test_update_from_result_success(self, tracker):
        """Test updating URL with successful result."""
        row = tracker.register_url(
            url="https://example.com",
            discovered_by="test"
        )
        
        result = BrowserExtractResult(
            url="https://example.com",
            success=True,
            status="printed",
            page_title="Test Page",
            content_length=1000,
            pdf_path="/path/to/file.pdf",
            screenshot_path="/path/to/screenshot.png",
            http_status=200,
            steps_taken=3,
            elapsed_seconds=15.0
        )
        
        tracker.update_from_result("https://example.com", result)
        
        with tracker.db.session() as session:
            updated = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com"
            ).first()
            assert updated.status == "printed"
            assert updated.title == "Test Page"
            assert updated.content_length == 1000
            assert updated.pdf_path == "/path/to/file.pdf"
            assert updated.printed_at is not None

    def test_update_from_result_failure(self, tracker):
        """Test updating URL with failure result."""
        row = tracker.register_url(
            url="https://example.com/blocked",
            discovered_by="test"
        )
        
        result = BrowserExtractResult(
            url="https://example.com/blocked",
            success=False,
            status="failed_4xx",
            http_status=403,
            failure_category="bot_detection",
            failure_detail="Access denied",
            is_retryable=False,
            steps_taken=1,
            elapsed_seconds=5.0
        )
        
        tracker.update_from_result("https://example.com/blocked", result)
        
        with tracker.db.session() as session:
            updated = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com/blocked"
            ).first()
            assert updated.status == "failed_4xx"
            assert updated.http_status == 403
            assert updated.failure_category == "bot_detection"
            assert updated.error_message == "Access denied"

    def test_get_review_queue(self, tracker):
        """Test getting review queue of unreviewed 4xx failures."""
        # Register some URLs with different statuses
        tracker.register_url("https://example.com/ok", "test")
        tracker.register_url("https://example.com/blocked1", "test")
        tracker.register_url("https://example.com/blocked2", "test")
        tracker.register_url("https://example.com/timeout", "test")
        
        # Update statuses
        with tracker.db.session() as session:
            # One successful
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com/ok"
            ).first()
            row.status = "printed"
            
            # Two 4xx failures (unreviewed)
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com/blocked1"
            ).first()
            row.status = "failed_4xx"
            
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com/blocked2"
            ).first()
            row.status = "failed_4xx"
            
            # One timeout (not 4xx)
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com/timeout"
            ).first()
            row.status = "failed_timeout"
            
            session.commit()
        
        review_queue = tracker.get_review_queue()
        assert len(review_queue) == 2
        assert all(row.status == "failed_4xx" for row in review_queue)
        assert all(row.reviewed is False for row in review_queue)

    def test_get_review_queue_with_limit(self, tracker):
        """Test getting review queue with limit."""
        for i in range(5):
            tracker.register_url(f"https://example.com/page{i}", "test")
        
        with tracker.db.session() as session:
            for i in range(5):
                row = session.query(SourceURLRow).filter(
                    SourceURLRow.url == f"https://example.com/page{i}"
                ).first()
                row.status = "failed_4xx"
            session.commit()
        
        review_queue = tracker.get_review_queue(limit=3)
        assert len(review_queue) == 3

    def test_mark_reviewed(self, tracker):
        """Test marking URL as reviewed."""
        row = tracker.register_url(
            url="https://example.com/blocked",
            discovered_by="test"
        )
        
        with tracker.db.session() as session:
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com/blocked"
            ).first()
            row.status = "failed_4xx"
            session.commit()
        
        tracker.mark_reviewed(
            "https://example.com/blocked",
            review_note="Will retry with different proxy"
        )
        
        with tracker.db.session() as session:
            updated = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com/blocked"
            ).first()
            assert updated.reviewed is True
            assert updated.review_note == "Will retry with different proxy"

    def test_retry_url(self, tracker):
        """Test retrying a URL."""
        row = tracker.register_url(
            url="https://example.com/timeout",
            discovered_by="test"
        )
        
        with tracker.db.session() as session:
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com/timeout"
            ).first()
            row.status = "failed_timeout"
            row.retry_count = 0
            session.commit()
        
        tracker.retry_url("https://example.com/timeout")
        
        with tracker.db.session() as session:
            updated = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com/timeout"
            ).first()
            assert updated.status == "pending"
            assert updated.retry_count == 1

    def test_retry_all_retryable(self, tracker):
        """Test retrying all retryable failures."""
        # Register URLs
        for i in range(3):
            tracker.register_url(f"https://example.com/page{i}", "test")
        
        # Set different statuses
        with tracker.db.session() as session:
            # 5xx (retryable)
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com/page0"
            ).first()
            row.status = "failed_5xx"
            row.is_retryable = True
            
            # Timeout (retryable)
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com/page1"
            ).first()
            row.status = "failed_timeout"
            row.is_retryable = True
            
            # 4xx (not retryable)
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com/page2"
            ).first()
            row.status = "failed_4xx"
            row.is_retryable = False
            
            session.commit()
        
        count = tracker.retry_all_retryable()
        assert count == 2
        
        # Verify statuses
        with tracker.db.session() as session:
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com/page0"
            ).first()
            assert row.status == "pending"
            
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com/page1"
            ).first()
            assert row.status == "pending"
            
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == "https://example.com/page2"
            ).first()
            assert row.status == "failed_4xx"

    def test_get_stats(self, tracker):
        """Test getting URL statistics."""
        # Register URLs
        for i in range(5):
            tracker.register_url(f"https://example.com/page{i}", "test")
        
        # Set different statuses
        with tracker.db.session() as session:
            statuses = ["pending", "printed", "failed_4xx", "failed_5xx", "blocked"]
            for i, status in enumerate(statuses):
                row = session.query(SourceURLRow).filter(
                    SourceURLRow.url == f"https://example.com/page{i}"
                ).first()
                row.status = status
                if i in [2, 3]:  # Add captcha detection
                    row.captcha_detected = True
                if i == 2:
                    row.captcha_solved = True
            session.commit()
        
        stats = tracker.get_stats()
        assert stats["total_urls"] == 5
        assert stats["status_counts"]["pending"] == 1
        assert stats["status_counts"]["printed"] == 1
        assert stats["status_counts"]["failed_4xx"] == 1
        assert stats["status_counts"]["failed_5xx"] == 1
        assert stats["status_counts"]["blocked"] == 1
        assert stats["captcha_detected"] == 2
        assert stats["captcha_solved"] == 1
        assert stats["review_queue_count"] == 1

    def test_get_pending_urls(self, tracker):
        """Test getting pending URLs."""
        # Register URLs
        for i in range(5):
            tracker.register_url(f"https://example.com/page{i}", "test")
        
        # Set some to non-pending
        with tracker.db.session() as session:
            for i in range(3):
                row = session.query(SourceURLRow).filter(
                    SourceURLRow.url == f"https://example.com/page{i}"
                ).first()
                row.status = "printed"
            session.commit()
        
        pending = tracker.get_pending_urls()
        assert len(pending) == 2
        assert "https://example.com/page3" in pending
        assert "https://example.com/page4" in pending

    def test_get_pending_urls_with_limit(self, tracker):
        """Test getting pending URLs with limit."""
        for i in range(5):
            tracker.register_url(f"https://example.com/page{i}", "test")
        
        pending = tracker.get_pending_urls(limit=3)
        assert len(pending) == 3


class TestCommunityURLLinks:
    """Tests for the community_urls many-to-many link table."""

    @pytest.fixture
    def temp_db(self, tmp_path):
        db = CommunityDatabase(db_path=tmp_path / "links.db")
        db.create_tables()
        return db

    @pytest.fixture
    def tracker(self, temp_db, tmp_path):
        return URLTracker(db=temp_db, pages_dir=tmp_path / "pages")

    def test_register_url_links_community(self, tracker):
        tracker.register_url(
            "https://example.com/a", "web_condenser", community_slug="alpha"
        )
        linked = tracker.get_urls_for_community("alpha")
        assert len(linked) == 1
        assert linked[0]["url"] == "https://example.com/a"

    def test_same_url_multiple_communities(self, tracker):
        tracker.register_url(
            "https://example.com/a", "web_condenser", community_slug="alpha"
        )
        tracker.register_url(
            "https://example.com/a", "web_condenser", community_slug="bravo"
        )
        # One canonical row, two community links.
        assert set(tracker.get_communities_for_url("https://example.com/a")) == {
            "alpha",
            "bravo",
        }
        assert len(tracker.get_urls_for_community("alpha")) == 1
        assert len(tracker.get_urls_for_community("bravo")) == 1

    def test_update_quality_marks_inspected(self, tracker):
        tracker.register_url(
            "https://example.com/a", "web_condenser", community_slug="alpha"
        )
        tracker.update_url_quality(
            "https://example.com/a",
            has_community_data=True,
            data_quality_score=77.5,
            data_types_found="fees,amenities",
            data_summary="summary",
            status="printed",
            community_slug="alpha",
        )
        linked = tracker.get_urls_for_community("alpha")
        assert linked[0]["inspected"] is True
        assert linked[0]["data_quality_score"] == 77.5
        assert linked[0]["has_community_data"] is True

    def test_register_urls_batch_links_community(self, tracker):
        rows = tracker.register_urls(
            ["https://example.com/a", "https://example.com/b"],
            "web_condenser",
            community_slug="alpha",
        )
        assert len(rows) == 2
        assert len(tracker.get_urls_for_community("alpha")) == 2

    def test_backfill_from_legacy_column(self, temp_db, tracker):
        # A legacy row recorded its community only via source_urls.community_slug.
        with temp_db.session() as session:
            session.add(
                SourceURLRow(
                    url="https://legacy.example/x",
                    domain="legacy.example",
                    status="printed",
                    discovered_by="legacy",
                    community_slug="legacy-community",
                )
            )
            session.commit()

        n = temp_db.backfill_community_urls()
        assert n == 1
        assert len(tracker.get_urls_for_community("legacy-community")) == 1
        # Idempotent: a second run adds nothing.
        assert temp_db.backfill_community_urls() == 0


class TestFailureClassification:
    """Test the failure classification logic from vision_browser_agent."""

    def test_classify_cloudflare_block(self):
        from modules.community.vision_browser_agent import _classify_failure
        category, retryable = _classify_failure(403, "Attention Required - Cloudflare challenge page", None)
        assert category == "cloudflare_block"
        assert retryable is False

    def test_classify_bot_detection(self):
        from modules.community.vision_browser_agent import _classify_failure
        category, retryable = _classify_failure(403, "Access Denied - automated access blocked", None)
        assert category == "bot_detection"
        assert retryable is False

    def test_classify_auth_required(self):
        from modules.community.vision_browser_agent import _classify_failure
        category, retryable = _classify_failure(401, "Please sign in to continue", None)
        assert category == "auth_required"
        assert retryable is False

    def test_classify_not_found(self):
        from modules.community.vision_browser_agent import _classify_failure
        category, retryable = _classify_failure(404, "Page not found", None)
        assert category == "not_found"
        assert retryable is False

    def test_classify_rate_limited(self):
        from modules.community.vision_browser_agent import _classify_failure
        category, retryable = _classify_failure(429, "Too many requests", None)
        assert category == "rate_limited"
        assert retryable is True

    def test_classify_rate_limited_from_content(self):
        from modules.community.vision_browser_agent import _classify_failure
        category, retryable = _classify_failure(None, "Rate limit exceeded, please wait", None)
        assert category == "rate_limited"
        assert retryable is True

    def test_classify_forbidden(self):
        from modules.community.vision_browser_agent import _classify_failure
        category, retryable = _classify_failure(403, "Forbidden", None)
        assert category == "forbidden"
        assert retryable is False

    def test_classify_bad_request(self):
        from modules.community.vision_browser_agent import _classify_failure
        category, retryable = _classify_failure(400, "Bad request", None)
        assert category == "bad_request"
        assert retryable is False

    def test_classify_geo_blocked(self):
        from modules.community.vision_browser_agent import _classify_failure
        category, retryable = _classify_failure(403, "This content is not available in your region", None)
        assert category == "geo_blocked"
        assert retryable is False

    def test_classify_timeout(self):
        from modules.community.vision_browser_agent import _classify_failure
        category, retryable = _classify_failure(None, None, "Request timeout after 30 seconds")
        assert category == "timeout"
        assert retryable is True

    def test_classify_network_error(self):
        from modules.community.vision_browser_agent import _classify_failure
        category, retryable = _classify_failure(None, None, "DNS resolution failed")
        assert category == "network_error"
        assert retryable is False

    def test_classify_server_error(self):
        from modules.community.vision_browser_agent import _classify_failure
        category, retryable = _classify_failure(500, "Internal Server Error", None)
        assert category == "unknown"
        assert retryable is True

    def test_classify_unknown_4xx(self):
        from modules.community.vision_browser_agent import _classify_failure
        category, retryable = _classify_failure(418, "I'm a teapot", None)
        # Should fall through to unknown since no specific indicator matches
        # 418 < 500, so not classified as server error, not retryable
        assert category == "unknown"
        assert retryable is False


class TestRobotFriendlyDetection:
    """Test robot-friendly detection functionality."""

    def test_is_known_robot_friendly_gov(self):
        """Test that .gov domains are recognized as robot-friendly."""
        from modules.community.url_tracker import URLTracker
        from modules.community.database import CommunityDatabase
        from pathlib import Path
        import tempfile
        
        with tempfile.TemporaryDirectory() as tmpdir:
            db = CommunityDatabase(Path(tmpdir) / "test.db")
            tracker = URLTracker(db)
            
            assert tracker._is_known_robot_friendly("www.census.gov")
            assert tracker._is_known_robot_friendly("data.census.gov")
            assert tracker._is_known_robot_friendly("floridarevenue.com")
            assert tracker._is_known_robot_friendly("arcgis.com")
            assert tracker._is_known_robot_friendly("github.com")
            assert not tracker._is_known_robot_friendly("www.example.com")

    @pytest.mark.asyncio
    async def test_check_robot_friendly_no_robots_txt(self):
        """Test robot-friendly check when robots.txt doesn't exist."""
        from modules.community.url_tracker import _check_robot_friendly
        import httpx
        from unittest.mock import AsyncMock, patch
        
        # Mock successful response with no bot-detection signals
        mock_response = httpx.Response(
            status_code=200,
            text="<html><body>Welcome to our site</body></html>"
        )
        
        with patch('httpx.AsyncClient.get') as mock_get:
            # robots.txt returns 404 (doesn't exist)
            mock_get.side_effect = [
                httpx.Response(status_code=404),  # robots.txt
                mock_response,  # actual page
            ]
            
            is_friendly, reason = await _check_robot_friendly("https://example.com/page")
            assert is_friendly is True
            assert "No bot-detection signals detected" in reason

    @pytest.mark.asyncio
    async def test_check_robot_friendly_with_cloudflare(self):
        """Test robot-friendly check when Cloudflare is detected."""
        from modules.community.url_tracker import _check_robot_friendly
        import httpx
        from unittest.mock import patch
        
        # Mock response with Cloudflare challenge
        mock_response = httpx.Response(
            status_code=200,
            text="<html><body>Checking your browser before accessing the site. Cloudflare challenge.</body></html>"
        )
        
        with patch('httpx.AsyncClient.get') as mock_get:
            mock_get.side_effect = [
                httpx.Response(status_code=404),  # robots.txt
                mock_response,  # actual page with Cloudflare
            ]
            
            is_friendly, reason = await _check_robot_friendly("https://example.com/page")
            assert is_friendly is False
            assert "cloudflare" in reason.lower()

    @pytest.mark.asyncio
    async def test_check_robot_friendly_403_response(self):
        """Test robot-friendly check when site returns 403."""
        from modules.community.url_tracker import _check_robot_friendly
        import httpx
        from unittest.mock import patch
        
        with patch('httpx.AsyncClient.get') as mock_get:
            mock_get.side_effect = [
                httpx.Response(status_code=404),  # robots.txt
                httpx.Response(status_code=403),  # actual page
            ]
            
            is_friendly, reason = await _check_robot_friendly("https://example.com/page")
            assert is_friendly is False
            assert "403" in reason

    def test_domain_robot_friendly_caching(self):
        """Test that robot-friendly status is cached at domain level."""
        from modules.community.url_tracker import URLTracker
        from modules.community.database import CommunityDatabase
        from pathlib import Path
        import tempfile
        from datetime import datetime, timezone
        
        with tempfile.TemporaryDirectory() as tmpdir:
            db = CommunityDatabase(Path(tmpdir) / "test.db")
            db.create_tables()  # Create tables first
            tracker = URLTracker(db)
            
            # Initially not cached
            assert tracker._get_domain_robot_friendly("example.com") is None
            
            # Add to cache
            tracker._domain_robot_cache["example.com"] = (True, datetime.now(timezone.utc))
            
            # Now should return cached value
            assert tracker._get_domain_robot_friendly("example.com") is True
            
            # Clean up database connection
            db.engine.dispose()

    @pytest.mark.asyncio
    async def test_process_url_routes_robot_friendly(self):
        """Test that process_url routes robot-friendly URLs to headless extraction."""
        from modules.community.url_tracker import URLTracker
        from modules.community.database import CommunityDatabase
        from modules.community.models import BrowserExtractResult
        from pathlib import Path
        import tempfile
        from unittest.mock import AsyncMock, patch
        
        with tempfile.TemporaryDirectory() as tmpdir:
            db = CommunityDatabase(Path(tmpdir) / "test.db")
            db.create_tables()
            tracker = URLTracker(db, Path(tmpdir) / "pages")
            
            # Register a URL
            row = tracker.register_url("https://example.com/page", "test")
            
            # Mock robot-friendly check to return True
            with patch.object(tracker, '_check_and_cache_robot_friendly', new_callable=AsyncMock) as mock_check:
                mock_check.return_value = True
                
                # Mock headless extraction
                with patch('modules.community.url_tracker._headless_extract', new_callable=AsyncMock) as mock_headless:
                    mock_headless.return_value = BrowserExtractResult(
                        url="https://example.com/page",
                        success=True,
                        status="printed",
                        content_length=1000,
                    )
                    
                    result = await tracker.process_url("https://example.com/page")
                    
                    # Should have called headless extraction
                    mock_headless.assert_called_once()
                    assert result.success is True
            
            # Clean up database connection
            db.engine.dispose()

    @pytest.mark.asyncio
    async def test_process_url_routes_non_robot_friendly(self):
        """Test that process_url routes non-robot-friendly URLs to vision browser."""
        from modules.community.url_tracker import URLTracker
        from modules.community.database import CommunityDatabase
        from modules.community.models import BrowserExtractResult
        from pathlib import Path
        import tempfile
        from unittest.mock import AsyncMock, patch
        
        with tempfile.TemporaryDirectory() as tmpdir:
            db = CommunityDatabase(Path(tmpdir) / "test.db")
            db.create_tables()
            tracker = URLTracker(db, Path(tmpdir) / "pages")
            
            # Register a URL
            row = tracker.register_url("https://example.com/page", "test")
            
            # Mock robot-friendly check to return False
            with patch.object(tracker, '_check_and_cache_robot_friendly', new_callable=AsyncMock) as mock_check:
                mock_check.return_value = False
                
                # Mock vision browser extraction
                with patch('modules.community.url_tracker.access_and_extract', new_callable=AsyncMock) as mock_vision:
                    mock_vision.return_value = BrowserExtractResult(
                        url="https://example.com/page",
                        success=True,
                        status="printed",
                        content_length=1000,
                    )
                    
                    result = await tracker.process_url("https://example.com/page")
                    
                    # Should have called vision browser extraction
                    mock_vision.assert_called_once()
                    assert result.success is True
            
            # Clean up database connection
            db.engine.dispose()


class TestDataQuality:
    """Tests for data quality calculation functionality."""

    def test_calculate_data_quality_no_content(self):
        """Test quality calculation with no content."""
        from modules.community.url_tracker import calculate_data_quality
        
        result = calculate_data_quality(None)
        assert result["has_community_data"] is False
        assert result["data_quality_score"] == 0.0
        assert result["data_types_found"] is None

    def test_calculate_data_quality_empty_content(self):
        """Test quality calculation with empty content."""
        from modules.community.url_tracker import calculate_data_quality
        
        result = calculate_data_quality("")
        assert result["has_community_data"] is False
        assert result["data_quality_score"] == 0.0
        assert result["data_types_found"] is None

    def test_calculate_data_quality_with_fees(self):
        """Test quality calculation detects fee information."""
        from modules.community.url_tracker import calculate_data_quality
        
        content = "HOA dues are $450 per month. Annual assessment is $2000."
        result = calculate_data_quality(content)
        
        assert result["has_community_data"] is True
        assert "fees" in result["data_types_found"]
        assert result["data_quality_score"] > 0
        assert "fee" in result["data_summary"].lower()

    def test_calculate_data_quality_with_amenities(self):
        """Test quality calculation detects amenity information."""
        from modules.community.url_tracker import calculate_data_quality
        
        content = "Community amenities include pool, tennis courts, and clubhouse."
        result = calculate_data_quality(content)
        
        assert result["has_community_data"] is True
        assert "amenities" in result["data_types_found"]
        assert result["data_quality_score"] > 0

    def test_calculate_data_quality_with_demographics(self):
        """Test quality calculation detects demographic information."""
        from modules.community.url_tracker import calculate_data_quality
        
        content = "The population is 5000 with a median age of 45 and median household income of $120,000."
        result = calculate_data_quality(content)
        
        assert result["has_community_data"] is True
        assert "demographics" in result["data_types_found"]
        assert result["data_quality_score"] > 0

    def test_calculate_data_quality_with_proximity(self):
        """Test quality calculation detects proximity information."""
        from modules.community.url_tracker import calculate_data_quality
        
        content = "Located 5 minutes from shopping and hospitals. Close to schools and libraries."
        result = calculate_data_quality(content)
        
        assert result["has_community_data"] is True
        assert "proximity" in result["data_types_found"]
        assert result["data_quality_score"] > 0

    def test_calculate_data_quality_multiple_types(self):
        """Test quality calculation with multiple data types."""
        from modules.community.url_tracker import calculate_data_quality
        
        content = """
        HOA dues are $450 per month with annual assessment of $2000.
        Community amenities include pool, golf course, tennis courts, and clubhouse.
        Population is 5000 with median age of 45.
        Located 5 minutes from shopping and hospitals.
        """
        result = calculate_data_quality(content)
        
        assert result["has_community_data"] is True
        assert "fees" in result["data_types_found"]
        assert "amenities" in result["data_types_found"]
        assert "demographics" in result["data_types_found"]
        assert "proximity" in result["data_types_found"]
        # Should have reasonable score with multiple data types
        assert result["data_quality_score"] >= 35

    def test_calculate_data_quality_irrelevant_content(self):
        """Test quality calculation with irrelevant content."""
        from modules.community.url_tracker import calculate_data_quality
        
        content = "This is a general website about web development and programming."
        result = calculate_data_quality(content)
        
        assert result["has_community_data"] is False
        assert result["data_quality_score"] == 0.0
        assert result["data_types_found"] is None

    def test_data_quality_score_range(self):
        """Test that quality score is within expected range."""
        from modules.community.url_tracker import calculate_data_quality
        
        # Test with comprehensive content
        content = """
        HOA fees are $500 monthly. Annual assessment $3000.
        Amenities: pool, tennis, golf, clubhouse, fitness center, walking trails.
        Population: 8000 residents. Median age: 42. Median income: $150,000.
        Close to shopping, hospitals, schools, and libraries within 10 minutes.
        """
        result = calculate_data_quality(content)
        
        # Score should be between 0 and 100
        assert 0 <= result["data_quality_score"] <= 100
        # With comprehensive content, should have reasonable score (actual score ~44)
        assert result["data_quality_score"] >= 40

    def test_data_quality_length_bonus(self):
        """Test that longer content gets quality bonus."""
        from modules.community.url_tracker import calculate_data_quality
        
        # Short content with fees
        short_content = "HOA dues are $450 per month."
        short_result = calculate_data_quality(short_content)
        
        # Longer content with fees and more context
        long_content = """
        HOA dues are $450 per month. The community maintains common areas,
        provides security, and covers water and sewer. Annual assessment is $2000
        for reserve fund contributions. Fee schedule includes special assessments
        for capital improvements.
        """
        long_result = calculate_data_quality(long_content)
        
        # Longer content should have higher score due to length bonus
        assert long_result["data_quality_score"] > short_result["data_quality_score"]
