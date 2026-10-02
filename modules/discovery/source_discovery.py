"""
Deliverable 1.1 — Source Discovery Engine
==========================================
Probes each registered Florida data source and produces a structured
JSON catalog of available endpoints / datasets.

Supported probe strategies
---------------------------
  socrata  → queries the SODA catalog API to list all datasets
  arcgis   → walks the ArcGIS REST /rest/services tree
  ckan     → queries CKAN /api/3/action/package_list
  direct   → records the known URL as-is (no dynamic enumeration)
  web      → crawls unstructured / gated sites via WebCrawler;
             converts DataSignals into CatalogEntries

Usage (CLI)
-----------
  python -m modules.discovery.source_discovery
  python -m modules.discovery.source_discovery --output data/catalogs/florida_sources.json
  python -m modules.discovery.source_discovery --categories property tax
  python -m modules.discovery.source_discovery --include-unstructured
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from .florida_sources import ALL_SOURCES, DataCategory, FloridaSource
from .web_crawler import WebCrawler, CrawlResult, DataSignal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models (plain dicts for now; Pydantic models added in Phase 3)
# ---------------------------------------------------------------------------

CatalogEntry = dict[str, Any]

DEFAULT_TIMEOUT = httpx.Timeout(30.0)
DEFAULT_HEADERS = {"User-Agent": "RealEstateMagnet/0.1 (research; contact: admin@example.com)"}


# ---------------------------------------------------------------------------
# Retry decorator shared by all probe methods
# ---------------------------------------------------------------------------

def _http_retry():
    return retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
        reraise=True,
    )


# ---------------------------------------------------------------------------
# Protocol probers
# ---------------------------------------------------------------------------

class SourceDiscoveryEngine:
    """
    Asynchronously probes all registered Florida data sources and builds
    a catalog of available datasets / endpoints.
    """

    def __init__(
        self,
        sources: list[FloridaSource] | None = None,
        concurrency: int = 5,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        include_unstructured: bool = False,
        web_auth_cookies: dict[str, dict[str, str]] | None = None,
    ) -> None:
        """
        Parameters
        ----------
        include_unstructured:
            When True, protocol="web" sources are also crawled via WebCrawler.
            Defaults to False to keep structured discovery fast.
        web_auth_cookies:
            Map of source name → {cookie_name: cookie_value} for gated sites.
            E.g. {"Stellar MLS": {"SMLSSID": "abc123"}}
        """
        all_sources = sources or ALL_SOURCES
        if include_unstructured:
            self.sources = all_sources
        else:
            self.sources = [s for s in all_sources if s.protocol != "web"]
        self.concurrency = concurrency
        self.timeout = timeout
        self.include_unstructured = include_unstructured
        self.web_auth_cookies: dict[str, dict[str, str]] = web_auth_cookies or {}
        self._semaphore: asyncio.Semaphore | None = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        categories: list[DataCategory] | None = None,
    ) -> list[CatalogEntry]:
        """
        Probe all sources (optionally filtered by category) and return
        a flat list of CatalogEntry dicts.
        """
        sources = self.sources
        if categories:
            sources = [s for s in sources if any(c in s.categories for c in categories)]

        self._semaphore = asyncio.Semaphore(self.concurrency)

        async with httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            timeout=self.timeout,
            follow_redirects=True,
        ) as client:
            tasks = [self._probe_source(client, source) for source in sources]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        catalog: list[CatalogEntry] = []
        for source, result in zip(sources, results):
            if isinstance(result, Exception):
                logger.warning("Failed to probe %s: %s", source.name, result)
                catalog.append(_error_entry(source, str(result)))
            else:
                catalog.extend(result)

        return catalog

    # ------------------------------------------------------------------
    # Per-source dispatch
    # ------------------------------------------------------------------

    async def _probe_source(
        self, client: httpx.AsyncClient, source: FloridaSource
    ) -> list[CatalogEntry]:
        async with self._semaphore:
            logger.info("Probing [%s] %s", source.protocol.upper(), source.name)
            if source.protocol == "socrata":
                return await self._probe_socrata(client, source)
            if source.protocol == "arcgis":
                return await self._probe_arcgis(client, source)
            if source.protocol == "ckan":
                return await self._probe_ckan(client, source)
            if source.protocol == "web":
                return await self._probe_web(source)
            # direct — just emit what we know
            return [_direct_entry(source)]

    # ------------------------------------------------------------------
    # Socrata probe
    # ------------------------------------------------------------------

    async def _probe_socrata(
        self, client: httpx.AsyncClient, source: FloridaSource
    ) -> list[CatalogEntry]:
        """
        Uses the SODA catalog endpoint: /api/catalog/v1?limit=200
        Returns one entry per dataset found.
        """
        domain = source.socrata_domain or _hostname(source.base_url)
        url = f"https://{domain}/api/catalog/v1"
        params = {"limit": 200, "offset": 0}
        entries: list[CatalogEntry] = []

        while True:
            data = await _get_json(client, url, params=params)
            results = data.get("results", [])
            for item in results:
                resource = item.get("resource", {})
                entries.append({
                    "source_name": source.name,
                    "protocol": "socrata",
                    "categories": source.categories,
                    "dataset_name": resource.get("name", ""),
                    "dataset_id": resource.get("id", ""),
                    "endpoint_url": f"https://{domain}/resource/{resource.get('id', '')}.json",
                    "description": resource.get("description", ""),
                    "row_count": resource.get("rows_updated_at"),
                    "updated_at": resource.get("updatedAt", ""),
                    "discovered_at": _now(),
                    "raw_metadata": item,
                })

            count = data.get("resultSetSize", 0)
            params["offset"] += len(results)
            if params["offset"] >= count or not results:
                break

        logger.info("  Socrata %s → %d datasets", domain, len(entries))
        return entries

    # ------------------------------------------------------------------
    # ArcGIS REST probe
    # ------------------------------------------------------------------

    async def _probe_arcgis(
        self, client: httpx.AsyncClient, source: FloridaSource
    ) -> list[CatalogEntry]:
        """
        Walks the ArcGIS REST /rest/services tree one level deep.
        Deep-walking all sub-folders is skipped to avoid hammering servers.
        """
        base = source.base_url.rstrip("/")
        services_url = f"{base}/rest/services"
        entries: list[CatalogEntry] = []

        try:
            data = await _get_json(client, services_url, params={"f": "json"})
        except Exception as exc:
            # Some portals mount services at the root directly
            logger.debug("ArcGIS root probe failed for %s: %s", source.name, exc)
            return [_direct_entry(source)]

        services = data.get("services", [])
        folders = data.get("folders", [])

        for svc in services:
            entries.append(_arcgis_service_entry(source, base, svc))

        # One level of folder expansion
        for folder in folders[:20]:  # cap to avoid runaway requests
            folder_url = f"{services_url}/{folder}"
            try:
                folder_data = await _get_json(client, folder_url, params={"f": "json"})
                for svc in folder_data.get("services", []):
                    entries.append(_arcgis_service_entry(source, base, svc, folder=folder))
            except Exception as exc:
                logger.debug("ArcGIS folder %s failed: %s", folder, exc)

        logger.info("  ArcGIS %s → %d services", source.name, len(entries))
        return entries or [_direct_entry(source)]

    # ------------------------------------------------------------------
    # CKAN probe
    # ------------------------------------------------------------------

    async def _probe_ckan(
        self, client: httpx.AsyncClient, source: FloridaSource
    ) -> list[CatalogEntry]:
        base = source.base_url.rstrip("/")
        list_url = f"{base}/api/3/action/package_list"

        data = await _get_json(client, list_url)
        package_ids: list[str] = data.get("result", [])
        entries: list[CatalogEntry] = []

        # Fetch metadata for first 50 packages (polite cap)
        for pkg_id in package_ids[:50]:
            show_url = f"{base}/api/3/action/package_show"
            try:
                pkg_data = await _get_json(client, show_url, params={"id": pkg_id})
                pkg = pkg_data.get("result", {})
                resources = pkg.get("resources", [])
                for res in resources:
                    entries.append({
                        "source_name": source.name,
                        "protocol": "ckan",
                        "categories": source.categories,
                        "dataset_name": pkg.get("title", pkg_id),
                        "dataset_id": pkg_id,
                        "endpoint_url": res.get("url", ""),
                        "format": res.get("format", ""),
                        "description": pkg.get("notes", ""),
                        "updated_at": res.get("last_modified", ""),
                        "discovered_at": _now(),
                        "raw_metadata": pkg,
                    })
            except Exception as exc:
                logger.debug("CKAN package %s failed: %s", pkg_id, exc)

        logger.info("  CKAN %s → %d resources", source.name, len(entries))
        return entries or [_direct_entry(source)]

    # ------------------------------------------------------------------
    # Web / unstructured probe (delegates to WebCrawler)
    # ------------------------------------------------------------------

    async def _probe_web(self, source: FloridaSource) -> list[CatalogEntry]:
        """
        Crawls an unstructured site and converts each DataSignal into a
        CatalogEntry.  Only signals with relevance_score >= 0.4 are emitted
        to keep the catalog actionable.
        """
        auth_cookies = self.web_auth_cookies.get(source.name)
        crawler = WebCrawler(
            max_pages=30,          # conservative cap per source
            crawl_delay=2.0,       # extra-polite for government / community sites
        )
        crawl_result: CrawlResult = await crawler.crawl(source, auth_cookies=auth_cookies)

        entries: list[CatalogEntry] = []
        MIN_RELEVANCE = 0.40

        for signal in crawl_result.signals:
            if signal.relevance_score < MIN_RELEVANCE:
                continue
            entries.append({
                "source_name": source.name,
                "protocol": "web",
                "categories": source.categories,
                "dataset_name": signal.anchor_text or source.name,
                "dataset_id": None,
                "endpoint_url": signal.url,
                "description": signal.context_snippet,
                "signal_type": signal.signal_type.value,
                "format_hint": signal.format_hint,
                "relevance_score": signal.relevance_score,
                "requires_auth": signal.requires_auth or source.requires_auth,
                "requires_js": source.requires_js,
                "pages_visited": crawl_result.pages_visited,
                "discovered_at": _now(),
                "raw_metadata": signal.extra,
            })

        # If no actionable signals found, still emit a placeholder entry
        # so the operator knows the source was visited
        if not entries:
            entry = _direct_entry(source)
            entry["protocol"] = "web"
            entry["signal_type"] = "none"
            entry["pages_visited"] = crawl_result.pages_visited
            entry["gated_urls"] = crawl_result.gated_urls
            if crawl_result.notes:
                entry["notes"] = crawl_result.notes
            entries.append(entry)

        logger.info(
            "  Web %s → %d pages, %d signals (≥%.1f)",
            source.name, crawl_result.pages_visited,
            len(entries), MIN_RELEVANCE,
        )
        return entries


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@_http_retry()
async def _get_json(
    client: httpx.AsyncClient, url: str, params: dict | None = None
) -> dict:
    response = await client.get(url, params=params)
    response.raise_for_status()
    return response.json()


def _hostname(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).hostname or url


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _direct_entry(source: FloridaSource) -> CatalogEntry:
    return {
        "source_name": source.name,
        "protocol": "direct",
        "categories": source.categories,
        "dataset_name": source.name,
        "dataset_id": None,
        "endpoint_url": source.base_url,
        "description": source.notes,
        "discovered_at": _now(),
        "raw_metadata": {},
    }


def _error_entry(source: FloridaSource, error: str) -> CatalogEntry:
    entry = _direct_entry(source)
    entry["error"] = error
    return entry


def _arcgis_service_entry(
    source: FloridaSource,
    base_url: str,
    svc: dict,
    folder: str = "",
) -> CatalogEntry:
    svc_name = svc.get("name", "")
    svc_type = svc.get("type", "")
    path = f"{folder}/{svc_name}" if folder else svc_name
    url = f"{base_url}/rest/services/{path}/{svc_type}"
    return {
        "source_name": source.name,
        "protocol": "arcgis",
        "categories": source.categories,
        "dataset_name": svc_name,
        "dataset_id": path,
        "endpoint_url": url,
        "service_type": svc_type,
        "description": "",
        "discovered_at": _now(),
        "raw_metadata": svc,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Florida data source discovery engine"
    )
    parser.add_argument(
        "--output",
        default="data/catalogs/florida_sources.json",
        help="Path to write the discovered catalog JSON",
    )
    parser.add_argument(
        "--categories",
        nargs="*",
        help="Filter by category (property, tax, flood, schools, crime, transit, geospatial)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Max parallel HTTP probes",
    )
    parser.add_argument(
        "--include-unstructured",
        action="store_true",
        help="Also crawl protocol=web sources (slower; uses WebCrawler)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    engine = SourceDiscoveryEngine(
        concurrency=args.concurrency,
        include_unstructured=args.include_unstructured,
    )
    catalog = await engine.run(categories=args.categories)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "generated_at": _now(),
                "total_entries": len(catalog),
                "entries": catalog,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    print(f"\nDiscovered {len(catalog)} endpoints → {output_path}")


if __name__ == "__main__":
    asyncio.run(_main())
