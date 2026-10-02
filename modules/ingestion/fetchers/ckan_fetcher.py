"""
CKAN API 3 fetcher — package_list → resource downloads.

Discovery:  GET {base}/api/3/action/package_list?limit=1000
Fetch:      GET {base}/api/3/action/package_show?id={pkg}
            → pick the first file-like resource (csv/xlsx/zip/json/url)
            → download its bytes via PoliteClient.
"""

from __future__ import annotations

import logging

from ...discovery.florida_sources import FloridaSource
from .base import BaseFetcher, DatasetInfo, FetchResult

logger = logging.getLogger(__name__)

# resource formats worth downloading (in preference order)
PREFERRED_FORMATS = ("csv", "json", "geojson", "xlsx", "zip", "shp", "tsv")


class CkanFetcher(BaseFetcher):
    protocol = "ckan"

    async def discover(self, source: FloridaSource) -> list[DatasetInfo]:
        base = source.base_url.rstrip("/")
        try:
            resp = await self.client.get(
                f"{base}/api/3/action/package_list", params={"limit": 1000}
            )
        except Exception as exc:
            logger.warning("CKAN package_list %s failed: %s", base, exc)
            return []
        if resp.status_code != 200:
            logger.warning("CKAN package_list %s -> HTTP %d", base, resp.status_code)
            return []
        try:
            names = resp.json().get("result", [])
        except ValueError:
            return []
        return [
            DatasetInfo(dataset_id=name, name=name, url="", format_hint="unknown")
            for name in names
        ]

    async def fetch(
        self, source: FloridaSource, dataset: DatasetInfo
    ) -> FetchResult:
        base = source.base_url.rstrip("/")
        result = FetchResult(
            source_name=source.name,
            dataset_id=dataset.dataset_id,
            format="unknown",
        )

        try:
            resp = await self.client.get(
                f"{base}/api/3/action/package_show",
                params={"id": dataset.dataset_id},
            )
        except Exception as exc:
            result.warnings.append(f"package_show failed: {exc}")
            return result
        if resp.status_code != 200:
            result.warnings.append(f"package_show -> HTTP {resp.status_code}")
            return result

        try:
            pkg = resp.json().get("result", {})
        except ValueError:
            result.warnings.append("package_show returned non-JSON")
            return result

        resource = _pick_resource(pkg.get("resources", []))
        if resource is None:
            result.warnings.append("no downloadable file resource on package")
            return result

        res_url = resource.get("url", "")
        fmt = (resource.get("format") or "").lower().lstrip(".") or "unknown"
        result.url = res_url
        result.format = fmt if fmt in ("csv", "json", "geojson", "xlsx", "zip", "shp", "tsv", "pdf") else "unknown"
        result.metadata["package_title"] = pkg.get("title", "")

        try:
            file_resp = await self.client.get(res_url)
        except Exception as exc:
            result.warnings.append(f"resource download failed: {exc}")
            return result
        if file_resp.status_code != 200:
            result.warnings.append(f"resource download -> HTTP {file_resp.status_code}")
            return result

        result.raw_bytes = file_resp.content
        if result.format == "csv":
            import csv as _csv, io as _io
            try:
                result.row_count = max(0, len(list(_csv.reader(_io.StringIO(file_resp.text)))) - 1)
            except _csv.Error:
                pass
        return result


def _pick_resource(resources: list[dict]) -> dict | None:
    """Prefer file resources with a known data format; else any url resource."""
    if not resources:
        return None
    by_format = {
        (r.get("format") or "").lower().lstrip("."): r
        for r in resources if r.get("url")
    }
    for fmt in PREFERRED_FORMATS:
        if fmt in by_format:
            return by_format[fmt]
    for r in resources:
        if r.get("url", "").startswith(("http://", "https://")):
            return r
    return None
