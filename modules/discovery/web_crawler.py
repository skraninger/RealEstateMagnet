"""
Deliverable 1.3 — Unstructured Web Crawler
============================================
Discovers data assets on sites that have no structured API:

  • HTML pages with data tables
  • Download links (.csv, .xlsx, .xls, .zip, .json, .geojson, .kml, .shp, .pdf)
  • Embedded JSON blobs in <script> tags
  • XHR / fetch() API call patterns in page JS
  • Login-gated portals (session/cookie injection)
  • JS-rendered pages (Playwright)

Each discovered asset is returned as a ``DataSignal`` with a relevance
score so the SourceDiscoveryEngine can prioritise what to harvest first.

Usage
-----
  # Static HTML site
  crawler = WebCrawler()
  result = await crawler.crawl(source)

  # Gated site — pass cookies obtained out-of-band
  result = await crawler.crawl(source, auth_cookies={"SMLSSID": "abc123"})

  # CLI
  python -m modules.discovery.web_crawler --url https://www.hoa-usa.com/florida/ --depth 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import urllib.robotparser
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .florida_sources import FloridaSource

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = "RealEstateMagnet/0.1 (research crawler; contact: admin@example.com)"

# File extensions that indicate downloadable data assets
DATA_EXTENSIONS = frozenset([
    ".csv", ".tsv", ".xlsx", ".xls", ".ods",
    ".json", ".geojson", ".kml", ".kmz",
    ".shp", ".zip",   # .zip often contains shapefiles or CSVs
    ".pdf",           # low-value but sometimes contains tables
    ".xml", ".gml",
])

# URL fragments / path segments that indicate a search/query result page
SEARCH_PATH_HINTS = re.compile(
    r"(search|query|lookup|find|result|report|download|export|data|records|parcel|folio|property)",
    re.IGNORECASE,
)

# Keywords in link anchor text that signal data relevance
DATA_ANCHOR_KEYWORDS = re.compile(
    r"(download|export|data|report|parcel|property|assessment|tax|flood|"
    r"crime|school|transit|hoa|cdd|budget|record|search|roster|list|csv|"
    r"excel|shapefile|geojson|map)",
    re.IGNORECASE,
)

# Patterns that indicate inline API calls in JavaScript source
XHR_PATTERNS = [
    re.compile(r"""fetch\s*\(\s*['"]([^'"]+)['"]"""),
    re.compile(r"""axios\.[a-z]+\s*\(\s*['"]([^'"]+)['"]"""),
    re.compile(r"""XMLHttpRequest[^;]*open\s*\([^,]+,\s*['"]([^'"]+)['"]"""),
    re.compile(r"""\$\.(?:get|post|ajax)\s*\(\s*['"]([^'"]+)['"]"""),  # jQuery
]

# Max pages visited per source to stay polite
MAX_PAGES_DEFAULT = 50
CRAWL_DELAY_SECONDS = 1.5   # polite delay between requests


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class SignalType(str, Enum):
    DOWNLOAD_LINK = "download_link"   # direct link to a data file
    HTML_TABLE    = "html_table"      # page contains a data table
    JSON_BLOB     = "json_blob"       # embedded JSON in <script> tag
    API_PATTERN   = "api_pattern"     # XHR/fetch call detected in JS
    FORM          = "form"            # search/filter form (needs interaction)
    PAGINATION    = "pagination"      # paginated result set


@dataclass
class DataSignal:
    url: str
    signal_type: SignalType
    format_hint: str             # "csv", "xlsx", "json", "html_table", "pdf", "unknown"
    relevance_score: float       # 0.0–1.0
    anchor_text: str = ""        # text of the link / label
    context_snippet: str = ""    # surrounding text (truncated)
    requires_auth: bool = False
    table_row_count: int = 0     # for html_table signals
    table_col_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "signal_type": self.signal_type.value,
            "format_hint": self.format_hint,
            "relevance_score": self.relevance_score,
            "anchor_text": self.anchor_text,
            "context_snippet": self.context_snippet[:200],
            "requires_auth": self.requires_auth,
            "table_row_count": self.table_row_count,
            "table_col_count": self.table_col_count,
            **self.extra,
        }


@dataclass
class CrawlResult:
    seed_url: str
    source_name: str
    pages_visited: int
    signals: list[DataSignal]
    gated_urls: list[str]       # returned 401/403
    robots_blocked: list[str]   # disallowed by robots.txt
    errors: list[str]
    notes: list[str] = field(default_factory=list)

    @property
    def top_signals(self) -> list[DataSignal]:
        return sorted(self.signals, key=lambda s: s.relevance_score, reverse=True)

    def to_dict(self) -> dict:
        return {
            "seed_url": self.seed_url,
            "source_name": self.source_name,
            "pages_visited": self.pages_visited,
            "total_signals": len(self.signals),
            "gated_urls": self.gated_urls,
            "robots_blocked": self.robots_blocked,
            "errors": self.errors[:20],
            "notes": self.notes,
            "signals": [s.to_dict() for s in self.top_signals],
        }


# ---------------------------------------------------------------------------
# Robots.txt cache
# ---------------------------------------------------------------------------

class RobotsCache:
    """Fetches and caches robots.txt per hostname."""

    def __init__(self) -> None:
        self._cache: dict[str, urllib.robotparser.RobotFileParser] = {}

    async def is_allowed(self, client: httpx.AsyncClient, url: str) -> bool:
        hostname = urlparse(url).netloc
        if hostname not in self._cache:
            robots_url = f"{urlparse(url).scheme}://{hostname}/robots.txt"
            parser = urllib.robotparser.RobotFileParser()
            try:
                resp = await client.get(robots_url, timeout=10)
                parser.parse(resp.text.splitlines())
            except Exception:
                # If we can't fetch robots.txt, assume allowed
                pass
            self._cache[hostname] = parser
        return self._cache[hostname].can_fetch(USER_AGENT, url)


# ---------------------------------------------------------------------------
# WebCrawler
# ---------------------------------------------------------------------------

class WebCrawler:
    """
    Crawls an unstructured website and returns a ``CrawlResult`` listing
    all discovered data signals.

    For JS-rendered pages set ``use_playwright=True`` or ensure the source
    has ``requires_js=True``; Playwright must be installed separately:
        pip install playwright && playwright install chromium
    """

    def __init__(
        self,
        max_pages: int = MAX_PAGES_DEFAULT,
        crawl_delay: float = CRAWL_DELAY_SECONDS,
        timeout: float = 30.0,
        respect_robots: bool = True,
    ) -> None:
        self.max_pages = max_pages
        self.crawl_delay = crawl_delay
        self.timeout = httpx.Timeout(timeout)
        self.respect_robots = respect_robots
        self._robots = RobotsCache()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def crawl(
        self,
        source: FloridaSource,
        auth_cookies: dict[str, str] | None = None,
    ) -> CrawlResult:
        """
        Crawl a FloridaSource with protocol="web".

        Parameters
        ----------
        source:
            The source to crawl. ``crawl_depth``, ``requires_js``, and
            ``crawl_seed_paths`` are read from the source definition.
        auth_cookies:
            Optional dict of cookie name→value pairs injected into every
            request.  Obtain these out-of-band (login flow) for gated sites.
        """
        seed_urls: list[str] = [source.base_url]
        for path in source.crawl_seed_paths:
            seed_urls.append(urljoin(source.base_url, path))

        result = CrawlResult(
            seed_url=source.base_url,
            source_name=source.name,
            pages_visited=0,
            signals=[],
            gated_urls=[],
            robots_blocked=[],
            errors=[],
        )

        if source.requires_auth and not auth_cookies:
            result.notes.append(
                f"Source requires authentication. Auth notes: {source.auth_notes or 'See source definition.'} "
                "Pass auth_cookies= to unlock full crawl."
            )

        if source.requires_js:
            await self._crawl_with_playwright(
                seed_urls, source.crawl_depth, result, auth_cookies
            )
        else:
            await self._crawl_with_httpx(
                seed_urls, source.crawl_depth, result, auth_cookies
            )

        return result

    async def crawl_url(
        self,
        url: str,
        depth: int = 1,
        requires_js: bool = False,
        auth_cookies: dict[str, str] | None = None,
    ) -> CrawlResult:
        """Crawl an arbitrary URL without a FloridaSource definition."""
        source = FloridaSource(
            name=url,
            base_url=url,
            protocol="web",
            categories=["misc"],
            crawl_depth=depth,
            requires_js=requires_js,
        )
        return await self.crawl(source, auth_cookies=auth_cookies)

    # ------------------------------------------------------------------
    # httpx-based crawler (static HTML)
    # ------------------------------------------------------------------

    async def _crawl_with_httpx(
        self,
        seed_urls: list[str],
        max_depth: int,
        result: CrawlResult,
        auth_cookies: dict[str, str] | None,
    ) -> None:
        cookies = httpx.Cookies(auth_cookies or {})
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout,
            follow_redirects=True,
            cookies=cookies,
        ) as client:
            visited: set[str] = set()
            queue: list[tuple[str, int]] = [(u, 0) for u in seed_urls]

            while queue and result.pages_visited < self.max_pages:
                url, depth = queue.pop(0)
                url = _normalise_url(url)
                if url in visited:
                    continue
                visited.add(url)

                if self.respect_robots and not await self._robots.is_allowed(client, url):
                    result.robots_blocked.append(url)
                    logger.debug("robots.txt blocked: %s", url)
                    continue

                logger.info("  Crawling (depth=%d) %s", depth, url)
                try:
                    resp = await client.get(url)
                except Exception as exc:
                    result.errors.append(f"{url}: {exc}")
                    continue

                result.pages_visited += 1

                if resp.status_code in (401, 403):
                    result.gated_urls.append(url)
                    logger.debug("Gated (HTTP %d): %s", resp.status_code, url)
                    continue

                if resp.status_code != 200:
                    result.errors.append(f"{url}: HTTP {resp.status_code}")
                    continue

                ct = resp.headers.get("content-type", "")
                if "html" in ct:
                    soup = BeautifulSoup(resp.content, "lxml")
                    new_signals = _extract_signals_from_html(url, soup, result.seed_url)
                    result.signals.extend(new_signals)

                    if depth < max_depth:
                        child_links = _extract_follow_links(url, soup, result.seed_url)
                        for link in child_links:
                            if link not in visited:
                                queue.append((link, depth + 1))

                await asyncio.sleep(self.crawl_delay)

    # ------------------------------------------------------------------
    # Playwright-based crawler (JS-rendered pages)
    # ------------------------------------------------------------------

    async def _crawl_with_playwright(
        self,
        seed_urls: list[str],
        max_depth: int,
        result: CrawlResult,
        auth_cookies: dict[str, str] | None,
    ) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            result.errors.append(
                "playwright not installed. Run: pip install playwright && playwright install chromium"
            )
            result.notes.append(
                "Falling back to httpx (JS rendering unavailable). "
                "Some dynamic content may be missed."
            )
            await self._crawl_with_httpx(seed_urls, max_depth, result, auth_cookies)
            return

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=USER_AGENT,
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )

            # Inject auth cookies if provided
            if auth_cookies:
                base_parsed = urlparse(seed_urls[0])
                cookie_domain = base_parsed.netloc
                for name, value in auth_cookies.items():
                    await context.add_cookies([{
                        "name": name, "value": value,
                        "domain": cookie_domain, "path": "/",
                    }])

            visited: set[str] = set()
            queue: list[tuple[str, int]] = [(u, 0) for u in seed_urls]

            while queue and result.pages_visited < self.max_pages:
                url, depth = queue.pop(0)
                url = _normalise_url(url)
                if url in visited:
                    continue
                visited.add(url)

                logger.info("  Playwright (depth=%d) %s", depth, url)
                try:
                    page = await context.new_page()
                    # Capture XHR requests made by the page
                    captured_api_urls: list[str] = []
                    page.on("request", lambda req: captured_api_urls.append(req.url)
                            if _looks_like_data_api(req.url) else None)

                    resp = await page.goto(url, timeout=int(self.timeout.read * 1000))
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception as exc:
                    result.errors.append(f"{url}: {exc}")
                    try:
                        await page.close()
                    except Exception:
                        pass
                    continue

                result.pages_visited += 1

                if resp and resp.status in (401, 403):
                    result.gated_urls.append(url)
                    await page.close()
                    continue

                html = await page.content()
                soup = BeautifulSoup(html, "lxml")
                new_signals = _extract_signals_from_html(url, soup, seed_urls[0])
                result.signals.extend(new_signals)

                # Emit API signals captured via network interception
                for api_url in captured_api_urls:
                    result.signals.append(DataSignal(
                        url=api_url,
                        signal_type=SignalType.API_PATTERN,
                        format_hint=_guess_format_from_url(api_url),
                        relevance_score=_score_api_url(api_url),
                        anchor_text="[XHR intercepted]",
                        context_snippet=f"Captured from page: {url}",
                    ))

                if depth < max_depth:
                    child_links = _extract_follow_links(url, soup, seed_urls[0])
                    for link in child_links:
                        if link not in visited:
                            queue.append((link, depth + 1))

                await page.close()
                await asyncio.sleep(self.crawl_delay)

            await context.close()
            await browser.close()


# ---------------------------------------------------------------------------
# Signal extraction helpers
# ---------------------------------------------------------------------------

def _extract_signals_from_html(
    page_url: str, soup: BeautifulSoup, seed_url: str
) -> list[DataSignal]:
    signals: list[DataSignal] = []
    base_host = urlparse(seed_url).netloc

    # 1. Download links
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        abs_url = urljoin(page_url, href)
        ext = _file_extension(abs_url)
        if ext in DATA_EXTENSIONS:
            anchor = a.get_text(strip=True)
            signals.append(DataSignal(
                url=abs_url,
                signal_type=SignalType.DOWNLOAD_LINK,
                format_hint=ext.lstrip("."),
                relevance_score=_score_download_link(abs_url, anchor),
                anchor_text=anchor[:200],
                context_snippet=_surrounding_text(a, chars=150),
            ))

    # 2. HTML tables with a meaningful number of rows
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 3:
            continue
        cols = max((len(r.find_all(["td", "th"])) for r in rows), default=0)
        if cols < 2:
            continue
        header_text = " ".join(th.get_text(strip=True)
                               for th in (rows[0].find_all(["th", "td"]) if rows else []))
        signals.append(DataSignal(
            url=page_url,
            signal_type=SignalType.HTML_TABLE,
            format_hint="html_table",
            relevance_score=_score_html_table(header_text, len(rows), cols),
            anchor_text=header_text[:200],
            table_row_count=len(rows),
            table_col_count=cols,
        ))

    # 3. Embedded JSON blobs in <script> tags
    for script in soup.find_all("script"):
        src = script.get("src", "")
        text = script.string or ""
        if src:
            continue   # external scripts handled separately
        # Look for likely data objects: large-ish JSON arrays
        for match in re.finditer(r"(?:var|let|const)\s+\w+\s*=\s*(\[[\s\S]{100,5000}\])", text):
            try:
                data = json.loads(match.group(1))
                if isinstance(data, list) and len(data) >= 5:
                    signals.append(DataSignal(
                        url=page_url,
                        signal_type=SignalType.JSON_BLOB,
                        format_hint="json",
                        relevance_score=0.65,
                        anchor_text="[Embedded JSON array]",
                        context_snippet=match.group(0)[:200],
                        extra={"item_count": len(data)},
                    ))
            except (json.JSONDecodeError, ValueError):
                pass

        # Look for XHR / fetch patterns in inline scripts
        for pattern in XHR_PATTERNS:
            for m in pattern.finditer(text):
                api_url = m.group(1)
                if api_url.startswith(("http", "/")):
                    abs_api = urljoin(page_url, api_url)
                    signals.append(DataSignal(
                        url=abs_api,
                        signal_type=SignalType.API_PATTERN,
                        format_hint=_guess_format_from_url(abs_api),
                        relevance_score=_score_api_url(abs_api),
                        anchor_text="[Inline JS API call]",
                        context_snippet=m.group(0)[:200],
                    ))

    # 4. Search / filter forms
    for form in soup.find_all("form"):
        action = form.get("action", page_url)
        abs_action = urljoin(page_url, action)
        method = form.get("method", "GET").upper()
        label = form.get_text(separator=" ", strip=True)[:200]
        if SEARCH_PATH_HINTS.search(abs_action) or DATA_ANCHOR_KEYWORDS.search(label):
            signals.append(DataSignal(
                url=abs_action,
                signal_type=SignalType.FORM,
                format_hint="unknown",
                relevance_score=0.55,
                anchor_text=label[:200],
                extra={"method": method, "inputs": _form_inputs(form)},
            ))

    # 5. Pagination hints (next page links)
    for a in soup.find_all("a", href=True):
        anchor = a.get_text(strip=True).lower()
        if anchor in ("next", "next page", ">>", "›", "2") or re.search(r"page=\d+", a["href"]):
            abs_url = urljoin(page_url, a["href"])
            signals.append(DataSignal(
                url=abs_url,
                signal_type=SignalType.PAGINATION,
                format_hint="html_table",
                relevance_score=0.40,
                anchor_text=anchor,
            ))
            break  # one pagination signal per page is enough

    return signals


def _extract_follow_links(
    page_url: str, soup: BeautifulSoup, seed_url: str
) -> list[str]:
    """
    Return internal links worth following: same host, hinting at data content.
    Excludes obvious non-data paths (images, CSS, JS, login pages, etc.).
    """
    base_host = urlparse(seed_url).netloc
    results: list[str] = []
    seen: set[str] = set()

    SKIP_EXTENSIONS = frozenset([
        ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg",
        ".ico", ".woff", ".woff2", ".ttf", ".mp4", ".mp3",
    ])
    SKIP_PATHS = re.compile(
        r"/(login|logout|signin|signup|register|cart|checkout|contact|"
        r"about|privacy|terms|sitemap|feed|rss)",
        re.IGNORECASE,
    )

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        abs_url = _normalise_url(urljoin(page_url, href))
        if abs_url in seen:
            continue
        seen.add(abs_url)

        parsed = urlparse(abs_url)
        if parsed.netloc != base_host:
            continue   # stay on same host
        ext = _file_extension(abs_url)
        if ext in SKIP_EXTENSIONS:
            continue
        if SKIP_PATHS.search(parsed.path):
            continue

        anchor = a.get_text(strip=True)
        path = parsed.path.lower()
        # Only follow links that look data-relevant
        if SEARCH_PATH_HINTS.search(path) or DATA_ANCHOR_KEYWORDS.search(anchor):
            results.append(abs_url)

    return results


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

# Property / FL-relevant keywords boost relevance
_RELEVANCE_KEYWORDS = re.compile(
    r"(parcel|folio|property|assessment|appraise|tax|flood|"
    r"crime|school|transit|hoa|cdd|budget|zoning|permit|deed|"
    r"mortgage|foreclosure|lien|record|florida|county|municipal)",
    re.IGNORECASE,
)


def _score_download_link(url: str, anchor: str) -> float:
    ext = _file_extension(url)
    base = {
        ".csv": 0.90, ".json": 0.88, ".geojson": 0.92, ".kml": 0.80,
        ".xlsx": 0.75, ".xls": 0.70, ".zip": 0.65, ".xml": 0.60,
        ".gml": 0.60, ".tsv": 0.85, ".ods": 0.65, ".pdf": 0.30,
        ".shp": 0.85,
    }.get(ext, 0.40)
    boost = 0.10 if _RELEVANCE_KEYWORDS.search(url + " " + anchor) else 0.0
    return min(1.0, base + boost)


def _score_html_table(header_text: str, rows: int, cols: int) -> float:
    base = min(0.75, 0.40 + rows * 0.005 + cols * 0.02)
    boost = 0.15 if _RELEVANCE_KEYWORDS.search(header_text) else 0.0
    return min(1.0, base + boost)


def _score_api_url(url: str) -> float:
    base = 0.60
    if _RELEVANCE_KEYWORDS.search(url):
        base += 0.20
    if any(kw in url.lower() for kw in ("json", "geojson", "csv", "api", "data", "resource")):
        base += 0.10
    return min(1.0, base)


# ---------------------------------------------------------------------------
# URL / format helpers
# ---------------------------------------------------------------------------

def _normalise_url(url: str) -> str:
    """Strip fragment, normalise trailing slash."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/") or "/", p.params, p.query, ""))


def _file_extension(url: str) -> str:
    path = urlparse(url).path
    dot_pos = path.rfind(".")
    slash_pos = path.rfind("/")
    if dot_pos > slash_pos:
        return path[dot_pos:].lower()
    return ""


def _guess_format_from_url(url: str) -> str:
    ext = _file_extension(url)
    if ext in (".json", ".geojson"):
        return "json"
    if ext in (".csv", ".tsv"):
        return "csv"
    if ext in (".xlsx", ".xls"):
        return "xlsx"
    if "json" in url.lower():
        return "json"
    if "csv" in url.lower():
        return "csv"
    return "unknown"


def _looks_like_data_api(url: str) -> bool:
    """Quick filter for Playwright request interception."""
    lower = url.lower()
    skip = ("google-analytics", "googletagmanager", "facebook", "twitter",
            ".css", ".js", ".png", ".jpg", ".gif", ".ico", "fonts.gstatic")
    if any(s in lower for s in skip):
        return False
    return bool(
        _RELEVANCE_KEYWORDS.search(url)
        or any(kw in lower for kw in ("api", "json", "csv", "data", "resource", "query"))
    )


def _surrounding_text(tag: Any, chars: int = 150) -> str:
    parent = tag.parent
    if parent:
        text = parent.get_text(separator=" ", strip=True)
        return text[:chars]
    return ""


def _form_inputs(form: Any) -> list[dict]:
    inputs = []
    for inp in form.find_all(["input", "select", "textarea"]):
        inputs.append({
            "name": inp.get("name", ""),
            "type": inp.get("type", "text"),
            "value": inp.get("value", ""),
        })
    return inputs[:20]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Web crawler for unstructured FL data sources")
    parser.add_argument("--url", required=True, help="Seed URL to crawl")
    parser.add_argument("--depth", type=int, default=1, help="Max crawl depth")
    parser.add_argument("--js", action="store_true", help="Use Playwright for JS rendering")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES_DEFAULT)
    parser.add_argument("--delay", type=float, default=CRAWL_DELAY_SECONDS,
                        help="Seconds between requests")
    parser.add_argument("--output", default=None, help="Write JSON output to file")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    crawler = WebCrawler(max_pages=args.max_pages, crawl_delay=args.delay)
    result = await crawler.crawl_url(args.url, depth=args.depth, requires_js=args.js)

    print(f"\nCrawl complete: {result.pages_visited} pages visited")
    print(f"  Signals    : {len(result.signals)}")
    print(f"  Gated URLs : {len(result.gated_urls)}")
    print(f"  Blocked    : {len(result.robots_blocked)}")
    print(f"  Errors     : {len(result.errors)}")
    print("\nTop signals by relevance:")
    for s in result.top_signals[:10]:
        print(f"  [{s.relevance_score:.2f}] {s.signal_type.value:18} {s.url[:80]}")

    if args.output:
        from pathlib import Path
        Path(args.output).write_text(
            json.dumps(result.to_dict(), indent=2, default=str), encoding="utf-8"
        )
        print(f"\nFull results → {args.output}")


if __name__ == "__main__":
    asyncio.run(_main())
