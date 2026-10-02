"""
Web fetcher (Deliverable 2.1, unstructured sites) — consumes Phase-1 signals.

Discovery reuses :class:`modules.discovery.web_crawler.WebCrawler`
(httpx or Playwright depending on ``source.requires_js``) and turns the
top ``DataSignal``s into harvestable datasets:

  • download_link → file dataset (fetched as verbatim bytes)
  • html_table    → table dataset (parsed to records at fetch time)
  • json_blob     → embedded-JSON dataset (extracted at fetch time)

Gated sources without ``auth_cookies`` yield a single auth-placeholder
dataset whose fetch reports the requirement — same behaviour as Phase 1.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ...discovery.florida_sources import FloridaSource
from ...discovery.web_crawler import (
    DATA_EXTENSIONS,
    WebCrawler,
    _file_extension,
)
from .base import BaseFetcher, DatasetInfo, FetchResult

logger = logging.getLogger(__name__)

MAX_DATASETS_PER_SOURCE = 25

_EMBEDDED_JSON_PATTERNS = [
    re.compile(r"(?:var|let|const)\s+\w+\s*=\s*(\[[\s\S]{100,5000}\])"),
    re.compile(r"(?:var|let|const)\s+\w+\s*=\s*(\{[\s\S]{100,5000}\})"),
]


class WebFetcher(BaseFetcher):
    protocol = "web"

    def __init__(self, client, crawler: WebCrawler | None = None,
                 auth_cookies: dict[str, str] | None = None) -> None:
        super().__init__(client)
        self.crawler = crawler or WebCrawler()
        self.auth_cookies = auth_cookies

    # ------------------------------------------------------------------
    # Discovery — crawl the site and convert signals to datasets
    # ------------------------------------------------------------------

    async def discover(self, source: FloridaSource) -> list[DatasetInfo]:
        if source.requires_auth and not self.auth_cookies:
            logger.info("%s requires auth; emitting placeholder", source.name)
            return [DatasetInfo(
                dataset_id="__auth_required__",
                name="AUTH REQUIRED — pass auth_cookies to WebFetcher",
                url="",
                format_hint="unknown",
                metadata={"auth_notes": source.auth_notes},
            )]

        crawl = await self.crawler.crawl(source, auth_cookies=self.auth_cookies)
        for note in crawl.notes:
            logger.info("%s: %s", source.name, note)

        datasets: list[DatasetInfo] = []
        table_idx = 0
        seen_urls: set[str] = set()

        for sig in crawl.top_signals[: MAX_DATASETS_PER_SOURCE * 3]:
            if len(datasets) >= MAX_DATASETS_PER_SOURCE:
                break
            stype = sig.signal_type.value

            if stype == "download_link":
                if sig.url in seen_urls:
                    continue
                seen_urls.add(sig.url)
                datasets.append(DatasetInfo(
                    dataset_id=sig.url.rsplit("/", 1)[-1][:80] or "file",
                    name=sig.anchor_text[:120] or sig.url.rsplit("/", 1)[-1],
                    url=sig.url,
                    format_hint=sig.format_hint,
                    metadata={"signal": stype},
                ))

            elif stype == "html_table" and sig.table_row_count >= 3:
                datasets.append(DatasetInfo(
                    dataset_id=f"table-{table_idx}@{urlparse(sig.url).netloc}",
                    name=sig.anchor_text[:120] or f"HTML table on {sig.url}",
                    url=sig.url,
                    format_hint="html_table",
                    metadata={
                        "signal": stype,
                        "table_index": table_idx,
                        "rows": sig.table_row_count,
                        "cols": sig.table_col_count,
                    },
                ))
                table_idx += 1

            elif stype == "json_blob" and sig.url not in seen_urls:
                seen_urls.add(sig.url)
                datasets.append(DatasetInfo(
                    dataset_id=f"json-blob@{urlparse(sig.url).netloc}",
                    name="Embedded JSON on page",
                    url=sig.url,
                    format_hint="json",
                    metadata={"signal": stype},
                ))

        return datasets

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    async def fetch(
        self, source: FloridaSource, dataset: DatasetInfo
    ) -> FetchResult:
        result = FetchResult(
            source_name=source.name,
            dataset_id=dataset.dataset_id,
            format="unknown",
            url=dataset.url,
        )

        if dataset.dataset_id == "__auth_required__":
            result.warnings.append(
                f"Source requires authentication. {dataset.metadata.get('auth_notes') or 'Pass auth_cookies to WebFetcher.'}"
            )
            return result

        # 1) Plain file download (download_link signals)
        if _file_extension(dataset.url) in DATA_EXTENSIONS:
            try:
                resp = await self.client.get(dataset.url)
            except Exception as exc:
                result.warnings.append(f"download failed: {exc}")
                return result
            if resp.status_code != 200:
                result.warnings.append(f"HTTP {resp.status_code} downloading {dataset.url}")
                return result
            result.raw_bytes = resp.content
            result.format = _file_extension(dataset.url).lstrip(".") or "unknown"
            return result

        # 2) Page-based extraction (html_table / json_blob)
        html = await self._page_html(source, dataset.url)
        if html is None:
            result.warnings.append(f"could not fetch page {dataset.url}")
            return result

        soup = BeautifulSoup(html, "lxml")
        sig = dataset.metadata.get("signal")

        if sig == "html_table":
            records = _extract_table_records(soup, dataset.metadata.get("table_index", 0))
            if not records:
                result.warnings.append("target table no longer present on page")
                return result
            result.records = records
            result.row_count = len(records)
            result.format = "html"

        elif sig == "json_blob":
            blobs = _extract_embedded_json(soup)
            if not blobs:
                result.warnings.append("no embedded JSON blobs found on page")
                return result
            records: list[dict] = []
            for blob in blobs:
                if isinstance(blob, list):
                    records.extend(r for r in blob if isinstance(r, dict))
                elif isinstance(blob, dict):
                    records.append(blob)
            if not records:
                result.warnings.append("embedded JSON contained no record objects")
                return result
            result.records = records
            result.row_count = len(records)
            result.format = "json"

        else:
            result.warnings.append(f"unsupported signal type {sig!r}")
        return result

    # ------------------------------------------------------------------
    # Page fetching (httpx or Playwright)
    # ------------------------------------------------------------------

    async def _page_html(self, source: FloridaSource, url: str) -> str | None:
        if source.requires_js:
            html = await self._playwright_html(url)
            if html is not None:
                return html
            logger.warning("Playwright failed for %s; falling back to httpx", url)
        try:
            resp = await self.client.get(url)
        except Exception as exc:
            logger.warning("page fetch %s failed: %s", url, exc)
            return None
        if resp.status_code != 200 or "html" not in resp.headers.get("content-type", "html"):
            return None
        return resp.text

    async def _playwright_html(self, url: str) -> str | None:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("playwright not installed; JS rendering unavailable")
            return None
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.goto(url, timeout=30000)
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    return await page.content()
                finally:
                    await browser.close()
        except Exception as exc:
            logger.warning("playwright render %s failed: %s", url, exc)
            return None


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _extract_table_records(soup: BeautifulSoup, table_index: int = 0) -> list[dict]:
    """Parse the Nth qualifying <table> into a list of row dicts."""
    tables = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 3:
            continue
        cols = max((len(r.find_all(["td", "th"])) for r in rows), default=0)
        if cols < 2:
            continue
        tables.append((rows, cols))

    if not tables:
        return []
    if table_index >= len(tables):
        tables.sort(key=lambda t: (len(t[0]), t[1]), reverse=True)
        rows, _ = tables[0]
    else:
        rows, _ = tables[table_index]

    header_cells = [c.get_text(strip=True) or f"col{i}"
                    for i, c in enumerate(rows[0].find_all(["th", "td"]))]
    records: list[dict] = []
    for row in rows[1:]:
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        if not any(cells):
            continue
        record = {header_cells[i]: (cells[i] if i < len(cells) else "")
                  for i in range(len(header_cells))}
        records.append(record)
    return records


def _extract_embedded_json(soup: BeautifulSoup) -> list:
    """Pull large JSON arrays/objects assigned to JS variables in inline scripts."""
    found: list = []
    for script in soup.find_all("script"):
        if script.get("src"):
            continue
        text = script.string or ""
        for pattern in _EMBEDDED_JSON_PATTERNS:
            for match in pattern.finditer(text):
                try:
                    found.append(json.loads(match.group(1)))
                except (json.JSONDecodeError, ValueError):
                    continue
    return found
