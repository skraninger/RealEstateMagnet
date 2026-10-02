"""
Direct file fetcher — known file URLs (NAL CSV/ZIP, FGDL shapefiles, ...).

Discovery:
  • base_url ending in a data extension → single dataset
  • base_url is an HTML page → extract download links via the Phase-1
    signal extractor (same host, data extensions only)

Fetch: verbatim bytes + sha256/size recorded by storage; HTTP 404 and
transport errors become warnings, never crashes.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup

from ...discovery.florida_sources import FloridaSource
from ...discovery.web_crawler import (
    DATA_EXTENSIONS,
    _extract_signals_from_html,
)
from .base import BaseFetcher, DatasetInfo, FetchResult

logger = logging.getLogger(__name__)


class DirectFetcher(BaseFetcher):
    protocol = "direct"

    async def discover(self, source: FloridaSource) -> list[DatasetInfo]:
        base = source.base_url.rstrip("/")
        ext = _file_extension(base)

        if ext in DATA_EXTENSIONS:
            return [DatasetInfo(
                dataset_id=base.rsplit("/", 1)[-1][:80] or "file",
                name=source.name,
                url=source.base_url,
                format_hint=ext.lstrip("."),
            )]

        # HTML page → harvest data-file links on the same host
        try:
            resp = await self.client.get(source.base_url)
        except Exception as exc:
            logger.warning("Direct discovery fetch %s failed: %s", base, exc)
            return []
        if resp.status_code != 200 or "html" not in resp.headers.get("content-type", "html"):
            logger.warning("Direct discovery %s -> HTTP %d", base, resp.status_code)
            return []

        soup = BeautifulSoup(resp.content, "lxml")
        signals = _extract_signals_from_html(source.base_url, soup, source.base_url)
        host = urlparse(base).netloc
        datasets: list[DatasetInfo] = []
        seen: set[str] = set()
        for sig in signals:
            if sig.signal_type.value != "download_link":
                continue
            if urlparse(sig.url).netloc != host:
                continue
            if sig.url in seen:
                continue
            seen.add(sig.url)
            datasets.append(DatasetInfo(
                dataset_id=sig.url.rsplit("/", 1)[-1][:80] or "file",
                name=sig.anchor_text[:120] or sig.url.rsplit("/", 1)[-1],
                url=sig.url,
                format_hint=sig.format_hint,
            ))
        return datasets

    async def fetch(
        self, source: FloridaSource, dataset: DatasetInfo
    ) -> FetchResult:
        result = FetchResult(
            source_name=source.name,
            dataset_id=dataset.dataset_id,
            format=_format_from_url(dataset.url),
            url=dataset.url,
        )
        try:
            resp = await self.client.get(dataset.url)
        except Exception as exc:
            result.warnings.append(f"download failed: {exc}")
            return result

        if resp.status_code == 404:
            result.warnings.append("HTTP 404 — file not found (may be seasonal/rotated)")
            return result
        if resp.status_code != 200:
            result.warnings.append(f"HTTP {resp.status_code} downloading {dataset.url}")
            return result

        result.raw_bytes = resp.content
        if "content-length" in resp.headers:
            result.metadata["declared_size"] = int(resp.headers["content-length"])
        if result.format == "csv":
            import csv as _csv, io as _io
            try:
                result.row_count = max(0, len(list(_csv.reader(_io.StringIO(resp.text)))) - 1)
            except _csv.Error:
                pass
        return result


def _file_extension(url: str) -> str:
    path = urlparse(url).path
    dot_pos = path.rfind(".")
    slash_pos = path.rfind("/")
    if dot_pos > slash_pos:
        return path[dot_pos:].lower()
    return ""


def _format_from_url(url: str) -> str:
    ext = _file_extension(url).lstrip(".")
    known = {"csv", "json", "geojson", "xlsx", "zip", "shp", "pdf", "tsv", "xml", "kml"}
    return ext if ext in known else ("unknown" if not ext else ext)
