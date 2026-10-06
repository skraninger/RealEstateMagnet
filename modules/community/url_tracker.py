"""URL tracking and review queue for research pipeline.

Tracks all URLs discovered during research in the source_urls table.
Provides a review queue for 4xx failures so they can be manually handled.
Integrates with the vision browser agent to access pages and save PDFs.

Robot-Friendly Detection:
- Checks robots.txt to see if automated access is allowed
- Performs fast HTTP probe to detect bot-detection signals
- Caches result per domain to avoid redundant checks
- Routes robot-friendly URLs through fast headless browser access
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from sqlalchemy import func, and_, or_

from .database import CommunityDatabase, SourceURLRow
from .models import BrowserExtractResult
from .vision_browser_agent import access_and_extract

logger = logging.getLogger(__name__)


DEFAULT_PAGES_DIR = Path("data/research_pages")


# Keywords for detecting different types of community data
FEES_KEYWORDS = [
    "hoa", "homeowners association", "dues", "monthly fee", "annual fee",
    "assessment", "cdd", "community development district", "fee schedule",
    "maintenance fee", "$", "per month", "per year", "annually"
]

AMENITIES_KEYWORDS = [
    "pool", "golf", "tennis", "clubhouse", "fitness", "gym", "playground",
    "park", "walking trails", "pickleball", "basketball", "soccer",
    "amenities", "community features", "recreation"
]

DEMOGRAPHICS_KEYWORDS = [
    "population", "median age", "median income", "household income",
    "demographics", "census", "residents", "families", "households",
    "age distribution", "income level"
]

PROXIMITY_KEYWORDS = [
    "shopping", "grocery", "hospital", "school", "library", "nearby",
    "close to", "minutes from", "location", "convenient", "access to",
    "distance", "miles"
]


def calculate_data_quality(page_content: Optional[str], community_slug: Optional[str] = None) -> dict:
    """Calculate data quality metrics based on page content.
    
    Analyzes the extracted text to determine:
    - Whether the page contains relevant community data
    - What types of data were found (fees, amenities, demographics, proximity)
    - A quality score (0-100) based on content richness and relevance
    
    Args:
        page_content: The extracted text content from the page
        community_slug: Optional community identifier for relevance checking
    
    Returns:
        dict with keys:
            - has_community_data: bool
            - data_quality_score: float (0-100)
            - data_types_found: str (comma-separated list)
            - data_summary: str
    """
    if not page_content:
        return {
            "has_community_data": False,
            "data_quality_score": 0.0,
            "data_types_found": None,
            "data_summary": "No content extracted"
        }
    
    content_lower = page_content.lower()
    content_length = len(page_content)
    
    # Detect data types by keyword matching
    data_types = []
    type_scores = {}
    
    # Check for fees data
    fees_count = sum(1 for kw in FEES_KEYWORDS if kw in content_lower)
    if fees_count >= 2:
        data_types.append("fees")
        # Score based on number of keyword matches and content about fees
        type_scores["fees"] = min(25, fees_count * 3)
    
    # Check for amenities data
    amenities_count = sum(1 for kw in AMENITIES_KEYWORDS if kw in content_lower)
    if amenities_count >= 2:
        data_types.append("amenities")
        type_scores["amenities"] = min(25, amenities_count * 2)
    
    # Check for demographics data
    demographics_count = sum(1 for kw in DEMOGRAPHICS_KEYWORDS if kw in content_lower)
    if demographics_count >= 2:
        data_types.append("demographics")
        type_scores["demographics"] = min(25, demographics_count * 3)
    
    # Check for proximity data
    proximity_count = sum(1 for kw in PROXIMITY_KEYWORDS if kw in content_lower)
    if proximity_count >= 2:
        data_types.append("proximity")
        type_scores["proximity"] = min(25, proximity_count * 2)
    
    # Calculate overall quality score
    has_community_data = len(data_types) > 0
    
    if not has_community_data:
        # No community data found
        data_quality_score = 0.0
        data_summary = "Page does not contain relevant community data"
    else:
        # Base score from data types found (up to 75 points)
        type_score = sum(type_scores.values())
        
        # Bonus for content length (up to 25 points)
        # More content generally means more detailed information
        length_bonus = min(25, content_length / 200)  # 1 point per 200 chars, max 25
        
        data_quality_score = type_score + length_bonus
        
        # Build summary
        type_descriptions = []
        if "fees" in data_types:
            type_descriptions.append(f"{fees_count} fee-related terms")
        if "amenities" in data_types:
            type_descriptions.append(f"{amenities_count} amenity mentions")
        if "demographics" in data_types:
            type_descriptions.append(f"{demographics_count} demographic terms")
        if "proximity" in data_types:
            type_descriptions.append(f"{proximity_count} proximity mentions")
        
        data_summary = f"Found {len(data_types)} data type(s): {', '.join(type_descriptions)}. Content length: {content_length} chars."
    
    return {
        "has_community_data": has_community_data,
        "data_quality_score": round(data_quality_score, 2),
        "data_types_found": ",".join(data_types) if data_types else None,
        "data_summary": data_summary
    }


async def _check_robot_friendly(url: str) -> tuple[bool, str]:
    """Check if a URL is robot-friendly by examining robots.txt and doing an HTTP probe.
    
    Returns:
        (is_robot_friendly, reason)
    """
    parsed = urlparse(url)
    domain = parsed.netloc
    robots_url = f"{parsed.scheme}://{domain}/robots.txt"
    
    # Step 1: Check robots.txt
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(robots_url, follow_redirects=True)
            if resp.status_code == 200:
                rp = RobotFileParser()
                rp.parse(resp.text.splitlines())
                # Check if our user agent is allowed
                # We use "*" as the user agent to be conservative
                if not rp.can_fetch("*", url):
                    return False, "robots.txt disallows access"
            elif resp.status_code == 404:
                # No robots.txt means all access is allowed
                pass
            else:
                logger.debug("robots.txt returned status %d for %s", resp.status_code, domain)
    except Exception as exc:
        logger.debug("Failed to fetch robots.txt for %s: %s", domain, exc)
        # If we can't fetch robots.txt, assume it's okay (many sites don't have one)
    
    # Step 2: Fast HTTP probe to detect bot-detection signals
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, follow_redirects=True, timeout=10.0)
            
            if resp.status_code in (403, 429):
                return False, f"HTTP {resp.status_code} - likely bot detection"
            
            if resp.status_code >= 400:
                return False, f"HTTP {resp.status_code}"
            
            # Check for common bot-detection indicators in the response
            content = resp.text.lower()
            bot_indicators = [
                "cloudflare",
                "cf-challenge",
                "attention required",
                "checking your browser",
                "access denied",
                "automated access",
                "bot detection",
                "captcha",
                "recaptcha",
                "hcaptcha",
            ]
            
            for indicator in bot_indicators:
                if indicator in content:
                    return False, f"Bot detection signal: {indicator}"
            
            # If we got here, the site appears robot-friendly
            return True, "No bot-detection signals detected"
    
    except httpx.TimeoutException:
        return False, "HTTP probe timeout"
    except httpx.ConnectError as exc:
        return False, f"Connection error: {exc}"
    except Exception as exc:
        logger.warning("HTTP probe failed for %s: %s", url, exc)
        return False, f"HTTP probe error: {exc}"


async def _headless_extract(
    url: str,
    community_slug: Optional[str] = None,
    pages_dir: Path = DEFAULT_PAGES_DIR,
) -> BrowserExtractResult:
    """Fast headless browser extraction for robot-friendly sites.
    
    Uses Playwright in headless mode (no LLM vision loop needed).
    Much faster than the vision agent for sites that allow automated access.
    """
    from playwright.async_api import async_playwright
    from .vision_browser_agent import (
        _generate_pdf_path,
        _generate_screenshot_path,
        _classify_failure,
    )
    import time
    import pdfplumber
    
    started = time.monotonic()
    viewport = {"width": 1280, "height": 900}
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    
    pdf_path: Optional[Path] = None
    screenshot_path: Optional[Path] = None
    page_content: Optional[str] = None
    page_title: Optional[str] = None
    http_status: Optional[int] = None
    error_msg: Optional[str] = None
    
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            
            try:
                context = await browser.new_context(
                    user_agent=user_agent,
                    viewport=viewport,
                    locale='en-US',
                )
                page = await context.new_page()
                
                # Navigate to URL
                response = await page.goto(url, wait_until="networkidle", timeout=30000)
                http_status = response.status if response else None
                
                # Wait a bit for JS to render
                await asyncio.sleep(2)
                
                # Extract content
                page_title = await page.title()
                page_content = await page.inner_text("body")
                
                # Save PDF
                pdf_path = _generate_pdf_path(url, community_slug, pages_dir)
                await page.pdf(
                    path=str(pdf_path),
                    format='A4',
                    print_background=True,
                    margin={'top': '0.5in', 'right': '0.5in', 'bottom': '0.5in', 'left': '0.5in'},
                )
                
                # Extract text from PDF
                try:
                    with pdfplumber.open(pdf_path) as pdf:
                        text_pages = []
                        for pdf_page in pdf.pages:
                            text = pdf_page.extract_text()
                            if text:
                                text_pages.append(text)
                        page_content = "\n\n".join(text_pages) if text_pages else page_content
                except Exception as exc:
                    logger.warning("PDF text extraction failed: %s", exc)
                
            finally:
                await browser.close()
    
    except httpx.TimeoutException:
        error_msg = "Navigation timeout"
    except httpx.ConnectError as exc:
        error_msg = f"Network error: {exc}"
    except Exception as exc:
        error_msg = f"Unexpected error: {exc}"
        logger.exception("Headless extraction failed for %s", url)
    
    # Determine final status
    if page_content and pdf_path:
        status = "printed"
        failure_category = None
        is_retryable = False
    elif error_msg:
        failure_category, is_retryable = _classify_failure(http_status, None, error_msg)
        if http_status and 400 <= http_status < 500:
            status = "failed_4xx"
        elif http_status and http_status >= 500:
            status = "failed_5xx"
        elif "timeout" in error_msg.lower():
            status = "failed_timeout"
        elif "network" in error_msg.lower():
            status = "failed_network"
        else:
            status = "blocked"
    else:
        status = "blocked"
        failure_category = "unknown"
        is_retryable = False
        error_msg = "Unknown failure"
    
    elapsed = time.monotonic() - started
    
    return BrowserExtractResult(
        url=url,
        success=(status == "printed"),
        status=status,
        page_title=page_title,
        page_content=page_content,
        content_length=len(page_content or ""),
        pdf_path=str(pdf_path) if pdf_path else None,
        screenshot_path=str(screenshot_path) if screenshot_path else None,
        http_status=http_status,
        failure_category=failure_category,
        failure_detail=error_msg,
        is_retryable=is_retryable,
        captcha_detected=False,
        captcha_solved=False,
        steps_taken=0,  # No vision loop steps
        elapsed_seconds=elapsed,
    )


class URLTracker:
    """Manages URL discovery, access, and review queue."""
    
    # Known robot-friendly domains (government, open data portals, etc.)
    KNOWN_ROBOT_FRIENDLY_DOMAINS = {
        ".gov",           # US government sites
        ".gov.au",        # Australian government
        ".gov.uk",        # UK government
        "census.gov",
        "data.census.gov",
        "factfinder.census.gov",
        "data.floridahealth.gov",
        "floridarevenue.com",
        "dos.myflorida.com",
        "arcgis.com",
        "opendata.arcgis.com",
        "services.arcgis.com",
        "github.com",     # GitHub raw content
        "raw.githubusercontent.com",
        "en.wikipedia.org",
        "www.census.gov",
    }
    
    def __init__(
        self,
        db: CommunityDatabase,
        pages_dir: Path = DEFAULT_PAGES_DIR,
    ):
        self.db = db
        self.pages_dir = pages_dir
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        
        # Domain-level cache for robot-friendly checks
        # Maps domain -> (is_robot_friendly, checked_at)
        self._domain_robot_cache: dict[str, tuple[bool, datetime]] = {}
    
    def _is_known_robot_friendly(self, domain: str) -> bool:
        """Check if a domain is in the known robot-friendly list."""
        for friendly_domain in self.KNOWN_ROBOT_FRIENDLY_DOMAINS:
            if domain.endswith(friendly_domain) or domain == friendly_domain:
                return True
        return False
    
    def _get_domain_robot_friendly(self, domain: str) -> Optional[bool]:
        """Get cached robot-friendly status for a domain, or None if not checked."""
        if domain in self._domain_robot_cache:
            is_friendly, _ = self._domain_robot_cache[domain]
            return is_friendly
        
        # Check database for any URL from this domain that has been checked
        with self.db.session() as session:
            row = session.query(SourceURLRow).filter(
                SourceURLRow.domain == domain,
                SourceURLRow.robots_txt_checked == True,
            ).first()
            
            if row:
                self._domain_robot_cache[domain] = (row.robot_friendly or False, datetime.now(timezone.utc))
                return row.robot_friendly or False
        
        return None
    
    async def _check_and_cache_robot_friendly(self, url: str) -> bool:
        """Check if a URL is robot-friendly and cache the result at domain level.
        
        If the domain is already known to be robot-friendly (from cache or DB),
        returns immediately without re-checking.
        """
        parsed = urlparse(url)
        domain = parsed.netloc
        
        # Check known list first
        if self._is_known_robot_friendly(domain):
            logger.info("Domain %s is known robot-friendly (hardcoded)", domain)
            self._domain_robot_cache[domain] = (True, datetime.now(timezone.utc))
            self._mark_domain_robot_friendly(domain, True)
            return True
        
        # Check cache
        cached = self._get_domain_robot_friendly(domain)
        if cached is not None:
            logger.info("Domain %s robot-friendly=%s (cached)", domain, cached)
            return cached
        
        # Perform the check
        is_friendly, reason = await _check_robot_friendly(url)
        logger.info("Robot-friendly check for %s: %s (%s)", domain, is_friendly, reason)
        
        # Cache the result
        self._domain_robot_cache[domain] = (is_friendly, datetime.now(timezone.utc))
        
        # Update the current URL row
        with self.db.session() as session:
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == url
            ).first()
            if row:
                row.robot_friendly = is_friendly
                row.robots_txt_checked = True
                session.commit()
        
        return is_friendly
    
    def _mark_domain_robot_friendly(self, domain: str, is_friendly: bool) -> None:
        """Mark all URLs for a domain as robot-friendly checked."""
        with self.db.session() as session:
            rows = session.query(SourceURLRow).filter(
                SourceURLRow.domain == domain,
                SourceURLRow.robots_txt_checked == False,
            ).all()
            for row in rows:
                row.robot_friendly = is_friendly
                row.robots_txt_checked = True
            session.commit()
    
    def register_url(
        self,
        url: str,
        discovered_by: str,
        community_slug: Optional[str] = None,
    ) -> SourceURLRow:
        """Register a URL for tracking. Returns existing row if already registered."""
        domain = urlparse(url).netloc
        
        with self.db.session() as session:
            # Check if URL already exists
            existing = session.query(SourceURLRow).filter(
                SourceURLRow.url == url
            ).first()
            
            if existing:
                logger.debug("URL already registered: %s", url)
                # Touch all attributes while session is still open to avoid DetachedInstanceError
                _ = existing.id, existing.url, existing.domain, existing.status
                return existing
            
            # Create new row
            row = SourceURLRow(
                url=url,
                domain=domain,
                status="pending",
                discovered_by=discovered_by,
                community_slug=community_slug,
                discovered_at=datetime.now(timezone.utc),
            )
            session.add(row)
            session.commit()
            
            # Refresh the row to get database-generated values
            session.refresh(row)
            
            # Touch all attributes while session is still open
            _ = row.id, row.url, row.domain, row.status
            
            logger.info("Registered new URL: %s (by %s)", url, discovered_by)
            return row
    
    def register_urls(
        self,
        urls: list[str],
        discovered_by: str,
        community_slug: Optional[str] = None,
    ) -> list[SourceURLRow]:
        """Register multiple URLs. Returns list of rows."""
        return [
            self.register_url(url, discovered_by, community_slug)
            for url in urls
        ]
    
    def mark_accessing(self, url: str) -> None:
        """Mark URL as currently being accessed."""
        with self.db.session() as session:
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == url
            ).first()
            if row:
                row.status = "accessed"
                row.accessed_at = datetime.now(timezone.utc)
                session.commit()
    
    def mark_captcha_solving(self, url: str) -> None:
        """Mark URL as currently solving CAPTCHA."""
        with self.db.session() as session:
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == url
            ).first()
            if row:
                row.status = "captcha_solving"
                row.captcha_detected = True
                session.commit()
    
    def update_from_result(self, url: str, result: BrowserExtractResult) -> None:
        """Update URL row with results from vision browser agent."""
        with self.db.session() as session:
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == url
            ).first()
            
            if not row:
                logger.warning("URL not found in tracker: %s", url)
                return
            
            row.status = result.status
            row.http_status = result.http_status
            row.failure_category = result.failure_category
            row.failure_detail = result.failure_detail
            row.is_retryable = result.is_retryable
            row.pdf_path = result.pdf_path
            row.screenshot_path = result.screenshot_path
            row.title = result.page_title
            row.content_length = result.content_length
            row.captcha_detected = result.captcha_detected
            row.captcha_solved = result.captcha_solved
            row.steps_taken = result.steps_taken
            
            # Data quality metrics
            row.has_community_data = result.has_community_data
            row.data_quality_score = result.data_quality_score
            row.data_types_found = result.data_types_found
            row.data_summary = result.data_summary
            
            if result.status == "printed":
                row.printed_at = datetime.now(timezone.utc)
            
            if result.status.startswith("failed_"):
                row.error_message = result.failure_detail
            
            session.commit()
            logger.info(
                "Updated URL %s: status=%s, captcha=%s/%s, data_quality=%s",
                url, result.status, result.captcha_detected, result.captcha_solved,
                result.data_quality_score
            )
    
    async def process_url(
        self,
        url: str,
        community_slug: Optional[str] = None,
        max_steps: int = 10,
    ) -> BrowserExtractResult:
        """Process a URL through the appropriate browser agent.
        
        Robot-friendly URLs use fast headless browser (no LLM vision loop).
        Non-robot-friendly URLs use the full vision browser agent with CAPTCHA handling.
        After extraction, calculates data quality metrics based on content.
        """
        # Ensure URL is registered
        row = self.register_url(url, "manual", community_slug)
        
        # Skip if already printed
        if row.status == "printed":
            logger.info("URL already printed, skipping: %s", url)
            return BrowserExtractResult(
                url=url,
                success=True,
                status="printed",
                pdf_path=row.pdf_path,
                content_length=row.content_length or 0,
                has_community_data=row.has_community_data or False,
                data_quality_score=row.data_quality_score or 0.0,
                data_types_found=row.data_types_found,
                data_summary=row.data_summary,
            )
        
        # Mark as accessing
        self.mark_accessing(url)
        
        # Check if URL is robot-friendly
        is_robot_friendly = await self._check_and_cache_robot_friendly(url)
        
        # Route based on robot-friendly status
        if is_robot_friendly:
            logger.info("Using fast headless access for robot-friendly URL: %s", url)
            try:
                result = await _headless_extract(
                    url=url,
                    community_slug=community_slug,
                    pages_dir=self.pages_dir,
                )
            except Exception as e:
                logger.error("Headless extraction failed for %s: %s", url, e)
                result = BrowserExtractResult(
                    url=url,
                    success=False,
                    status="failed_network",
                    failure_detail=f"Headless extraction error: {e}",
                    failure_category="network_error",
                )
        else:
            logger.info("Using vision browser agent for non-robot-friendly URL: %s", url)
            try:
                result = await access_and_extract(
                    url=url,
                    community_slug=community_slug,
                    max_steps=max_steps,
                    pages_dir=self.pages_dir,
                )
            except Exception as e:
                logger.error("Vision browser agent failed for %s: %s", url, e)
                result = BrowserExtractResult(
                    url=url,
                    success=False,
                    status="blocked",
                    failure_detail=f"Vision browser error: {e}",
                    failure_category="unknown",
                )
        
        # Calculate data quality metrics if extraction succeeded
        if result.success and result.page_content:
            quality_metrics = calculate_data_quality(result.page_content, community_slug)
            result.has_community_data = quality_metrics["has_community_data"]
            result.data_quality_score = quality_metrics["data_quality_score"]
            result.data_types_found = quality_metrics["data_types_found"]
            result.data_summary = quality_metrics["data_summary"]
            
            logger.info(
                "Data quality for %s: score=%.1f, types=%s, has_data=%s",
                url, result.data_quality_score, 
                result.data_types_found or "none",
                result.has_community_data
            )
        
        # Update tracker with result
        self.update_from_result(url, result)
        
        return result
    
    def get_review_queue(
        self,
        limit: Optional[int] = None,
    ) -> list[SourceURLRow]:
        """Get URLs that failed with 4xx errors and haven't been reviewed yet."""
        with self.db.session() as session:
            query = session.query(SourceURLRow).filter(
                and_(
                    SourceURLRow.status == "failed_4xx",
                    SourceURLRow.reviewed == False,
                )
            ).order_by(
                SourceURLRow.discovered_at.desc()
            )
            
            if limit:
                query = query.limit(limit)
            
            return query.all()
    
    def get_high_quality_urls(
        self,
        min_score: float = 50.0,
        limit: Optional[int] = None,
    ) -> list[SourceURLRow]:
        """Get URLs with high data quality scores."""
        with self.db.session() as session:
            query = session.query(SourceURLRow).filter(
                and_(
                    SourceURLRow.has_community_data == True,
                    SourceURLRow.data_quality_score >= min_score,
                )
            ).order_by(
                SourceURLRow.data_quality_score.desc()
            )
            
            if limit:
                query = query.limit(limit)
            
            return query.all()
    
    def get_retryable_failures(
        self,
        limit: Optional[int] = None,
    ) -> list[SourceURLRow]:
        """Get failed URLs that are retryable (5xx, timeout, network errors)."""
        with self.db.session() as session:
            query = session.query(SourceURLRow).filter(
                and_(
                    or_(
                        SourceURLRow.status == "failed_5xx",
                        SourceURLRow.status == "failed_timeout",
                        SourceURLRow.status == "failed_network",
                    ),
                    SourceURLRow.is_retryable == True,
                )
            ).order_by(
                SourceURLRow.discovered_at.desc()
            )
            
            if limit:
                query = query.limit(limit)
            
            return query.all()
    
    def mark_reviewed(
        self,
        url: str,
        review_note: Optional[str] = None,
    ) -> None:
        """Mark a URL as reviewed with optional note."""
        with self.db.session() as session:
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == url
            ).first()
            if row:
                row.reviewed = True
                if review_note:
                    row.review_note = review_note
                session.commit()
                logger.info("Marked URL as reviewed: %s", url)
    
    def retry_url(self, url: str) -> None:
        """Reset a URL to pending status for retry."""
        with self.db.session() as session:
            row = session.query(SourceURLRow).filter(
                SourceURLRow.url == url
            ).first()
            if row:
                row.status = "pending"
                row.retry_count = (row.retry_count or 0) + 1
                row.reviewed = False
                row.review_note = None
                row.error_message = None
                session.commit()
                logger.info("Reset URL for retry: %s (attempt %d)", url, row.retry_count)
    
    def retry_all_retryable(self) -> int:
        """Reset all retryable failures to pending. Returns count of reset URLs."""
        rows = self.get_retryable_failures()
        for row in rows:
            self.retry_url(row.url)
        return len(rows)
    
    def get_stats(self) -> dict:
        """Get statistics about tracked URLs."""
        with self.db.session() as session:
            total = session.query(func.count(SourceURLRow.id)).scalar() or 0
            
            status_counts = {}
            for status in ["pending", "accessed", "captcha_solving", "printed", 
                          "failed_4xx", "failed_5xx", "failed_timeout", 
                          "failed_network", "blocked"]:
                count = session.query(func.count(SourceURLRow.id)).filter(
                    SourceURLRow.status == status
                ).scalar() or 0
                if count > 0:
                    status_counts[status] = count
            
            captcha_count = session.query(func.count(SourceURLRow.id)).filter(
                SourceURLRow.captcha_detected == True
            ).scalar() or 0
            
            captcha_solved = session.query(func.count(SourceURLRow.id)).filter(
                SourceURLRow.captcha_solved == True
            ).scalar() or 0
            
            review_queue_count = session.query(func.count(SourceURLRow.id)).filter(
                and_(
                    SourceURLRow.status == "failed_4xx",
                    SourceURLRow.reviewed == False,
                )
            ).scalar() or 0
        
        return {
            "total_urls": total,
            "status_counts": status_counts,
            "captcha_detected": captcha_count,
            "captcha_solved": captcha_solved,
            "review_queue_count": review_queue_count,
        }
    
    def get_pending_urls(self, limit: Optional[int] = None) -> list[str]:
        """Get list of pending URLs to process."""
        with self.db.session() as session:
            query = session.query(SourceURLRow.url).filter(
                SourceURLRow.status == "pending"
            ).order_by(
                SourceURLRow.discovered_at.asc()
            )
            
            if limit:
                query = query.limit(limit)
            
            return [row[0] for row in query.all()]


def main():
    """CLI for URL review and management."""
    import argparse
    import sys
    from rich.console import Console
    from rich.table import Table
    
    parser = argparse.ArgumentParser(
        description="Review and manage tracked URLs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Show review queue (unreviewed 4xx failures)
  python -m modules.community.url_tracker --review
  
  # Show review queue with limit
  python -m modules.community.url_tracker --review --limit 20
  
  # Show stats
  python -m modules.community.url_tracker --stats
  
  # Show retryable failures
  python -m modules.community.url_tracker --retryable
  
  # Mark a URL as reviewed
  python -m modules.community.url_tracker --mark-reviewed "https://example.com/page" --note "Will retry with proxy"
  
  # Retry a specific URL
  python -m modules.community.url_tracker --retry "https://example.com/page"
  
  # Retry all retryable failures
  python -m modules.community.url_tracker --retry-all
        """
    )
    
    parser.add_argument(
        "--db-path",
        default="data/communities.db",
        help="Path to database file (default: data/communities.db)"
    )
    
    action_group = parser.add_mutually_exclusive_group(required=True)
    action_group.add_argument(
        "--review",
        action="store_true",
        help="Show review queue (unreviewed 4xx failures)"
    )
    action_group.add_argument(
        "--retryable",
        action="store_true",
        help="Show retryable failures (5xx, timeout, network errors)"
    )
    action_group.add_argument(
        "--stats",
        action="store_true",
        help="Show URL tracking statistics"
    )
    action_group.add_argument(
        "--mark-reviewed",
        metavar="URL",
        help="Mark a URL as reviewed"
    )
    action_group.add_argument(
        "--retry",
        metavar="URL",
        help="Reset a URL to pending for retry"
    )
    action_group.add_argument(
        "--retry-all",
        action="store_true",
        help="Reset all retryable failures to pending"
    )
    action_group.add_argument(
        "--quality",
        action="store_true",
        help="Show URLs sorted by data quality score (highest first)"
    )
    
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of results"
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=50.0,
        help="Minimum data quality score to display (default: 50.0)"
    )
    parser.add_argument(
        "--note",
        help="Note to add when marking as reviewed"
    )
    
    args = parser.parse_args()
    
    # Initialize database and tracker
    db = CommunityDatabase(args.db_path)
    db.create_tables()  # Ensure all tables exist (idempotent)
    tracker = URLTracker(db)
    console = Console()
    
    if args.review:
        queue = tracker.get_review_queue(limit=args.limit)
        if not queue:
            console.print("[green]OK - No URLs in review queue[/green]")
            return
        
        table = Table(title=f"Review Queue ({len(queue)} URLs)")
        table.add_column("URL", style="cyan", no_wrap=True, max_width=80)
        table.add_column("Domain", style="green")
        table.add_column("Status", style="yellow")
        table.add_column("HTTP", style="red")
        table.add_column("Failure", style="magenta")
        table.add_column("Discovered", style="blue")
        
        for row in queue:
            table.add_row(
                row.url,
                row.domain or "",
                row.status,
                str(row.http_status or ""),
                row.failure_category or "",
                row.discovered_at.strftime("%Y-%m-%d %H:%M") if row.discovered_at else ""
            )
        
        console.print(table)
        
        # Show failure details for first few
        if len(queue) > 0:
            console.print("\n[yellow]Failure Details (first 3):[/yellow]")
            for i, row in enumerate(queue[:3]):
                if row.failure_detail:
                    console.print(f"\n[cyan]{i+1}. {row.url}[/cyan]")
                    console.print(f"   [dim]{row.failure_detail[:200]}[/dim]")
    
    elif args.retryable:
        rows = tracker.get_retryable_failures(limit=args.limit)
        if not rows:
            console.print("[green]OK - No retryable failures[/green]")
            return
        
        table = Table(title=f"Retryable Failures ({len(rows)} URLs)")
        table.add_column("URL", style="cyan", no_wrap=True, max_width=80)
        table.add_column("Status", style="yellow")
        table.add_column("HTTP", style="red")
        table.add_column("Failure", style="magenta")
        table.add_column("Retry Count", style="blue")
        
        for row in rows:
            table.add_row(
                row.url,
                row.status,
                str(row.http_status or ""),
                row.failure_category or "",
                str(row.retry_count or 0)
            )
        
        console.print(table)
    
    elif args.quality:
        rows = tracker.get_high_quality_urls(
            min_score=args.min_score,
            limit=args.limit
        )
        if not rows:
            console.print(f"[green]OK - No URLs found with quality score >= {args.min_score}[/green]")
            return
        
        table = Table(title=f"High Quality URLs ({len(rows)} URLs, min score: {args.min_score})")
        table.add_column("Quality", style="green", justify="right")
        table.add_column("URL", style="cyan", no_wrap=True, max_width=80)
        table.add_column("Data Types", style="yellow")
        table.add_column("Domain", style="blue")
        table.add_column("Community", style="magenta")
        
        for row in rows:
            table.add_row(
                f"{row.data_quality_score:.1f}",
                row.url,
                row.data_types_found or "",
                row.domain,
                row.community_slug or ""
            )
        
        console.print(table)
    
    elif args.stats:
        stats = tracker.get_stats()
        
        table = Table(title="URL Tracking Statistics")
        table.add_column("Metric", style="cyan")
        table.add_column("Count", style="green", justify="right")
        
        table.add_row("Total URLs", str(stats["total_urls"]))
        
        for status, count in stats["status_counts"].items():
            table.add_row(f"  {status}", str(count))
        
        table.add_row("CAPTCHA Detected", str(stats["captcha_detected"]))
        table.add_row("CAPTCHA Solved", str(stats["captcha_solved"]))
        
        solve_rate = 0
        if stats["captcha_detected"] > 0:
            solve_rate = (stats["captcha_solved"] / stats["captcha_detected"]) * 100
        table.add_row("CAPTCHA Solve Rate", f"{solve_rate:.1f}%")
        
        table.add_row("Review Queue", str(stats["review_queue_count"]))
        
        console.print(table)
    
    elif args.mark_reviewed:
        tracker.mark_reviewed(args.mark_reviewed, review_note=args.note)
        console.print(f"[green]OK - Marked as reviewed: {args.mark_reviewed}[/green]")
        if args.note:
            console.print(f"[dim]Note: {args.note}[/dim]")
    
    elif args.retry:
        tracker.retry_url(args.retry)
        console.print(f"[green]OK - Reset for retry: {args.retry}[/green]")
    
    elif args.retry_all:
        count = tracker.retry_all_retryable()
        console.print(f"[green]OK - Reset {count} URLs for retry[/green]")


if __name__ == "__main__":
    main()
