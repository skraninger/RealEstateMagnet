"""
Socrata (SODA) fetcher — opendata.broward.org, opendata.coj.net, ...

Discovery:  GET {base}/api/views?$limit=5000
Fetch:      GET {base}/api/views/{id}/rows.csv?$limit=-1   (full export)
            with paged fallback ($limit/$offset) if the portal rejects it.
"""

from __future__ import annotations

import csv
import io
import logging

from ...discovery.florida_sources import FloridaSource
from .base import BaseFetcher, DatasetInfo, FetchResult

logger = logging.getLogger(__name__)

PAGE_SIZE = 5000


class SocrataFetcher(BaseFetcher):
    protocol = "socrata"

    async def discover(self, source: FloridaSource) -> list[DatasetInfo]:
        base = source.base_url.rstrip("/")
        try:
            resp = await self.client.get(
                f"{base}/api/views", params={"$limit": 5000}
            )
        except Exception as exc:
            logger.warning("Socrata catalog %s failed: %s", base, exc)
            return []
        if resp.status_code != 200:
            logger.warning("Socrata catalog %s -> HTTP %d", base, resp.status_code)
            return []
        datasets: list[DatasetInfo] = []
        for item in resp.json():
            ds_id = item.get("id") or item.get("slug")
            if not ds_id:
                continue
            datasets.append(DatasetInfo(
                dataset_id=ds_id,
                name=item.get("name", ds_id),
                url=f"{base}/api/views/{ds_id}/rows.csv?$limit=-1",
                format_hint="csv",
                metadata={
                    "category": item.get("category", ""),
                    "rows_updated": item.get("rowsUpdatedAt"),
                },
            ))
        return datasets

    async def fetch(
        self, source: FloridaSource, dataset: DatasetInfo
    ) -> FetchResult:
        base = source.base_url.rstrip("/")
        result = FetchResult(
            source_name=source.name,
            dataset_id=dataset.dataset_id,
            format="csv",
            url=dataset.url or f"{base}/api/views/{dataset.dataset_id}/rows.csv?$limit=-1",
        )

        # 1) Try full export in one request
        try:
            resp = await self.client.get(
                f"{base}/api/views/{dataset.dataset_id}/rows.csv",
                params={"$limit": -1},
            )
        except Exception as exc:
            result.warnings.append(f"full export failed: {exc}")
            resp = None

        if resp is not None and resp.status_code == 200 and "csv" in resp.headers.get("content-type", "csv"):
            result.raw_bytes = resp.content
            result.row_count = _count_csv_rows(resp.text)
            return result

        # 2) Paged fallback
        if resp is not None:
            result.warnings.append(
                f"full export rejected (HTTP {resp.status_code}); falling back to paging"
            )
        chunks: list[str] = []
        total_rows = 0
        offset = 0
        try:
            while True:
                page = await self.client.get(
                    f"{base}/api/views/{dataset.dataset_id}/rows.csv",
                    params={"$limit": PAGE_SIZE, "$offset": offset},
                )
                if page.status_code != 200:
                    result.warnings.append(f"page at offset {offset} -> HTTP {page.status_code}")
                    break
                text = page.text
                rows_on_page = _count_csv_rows(text)
                if chunks:
                    # strip the repeated header row from subsequent pages
                    first_nl = text.find("\n")
                    text = text[first_nl + 1:] if first_nl != -1 else ""
                chunks.append(text)
                total_rows += rows_on_page
                if rows_on_page < PAGE_SIZE:
                    break
                offset += PAGE_SIZE
        except Exception as exc:
            result.warnings.append(f"paging aborted: {exc}")

        if chunks:
            body = "\n".join(c for c in chunks if c)
            result.raw_bytes = body.encode("utf-8")
            result.row_count = total_rows
        else:
            result.warnings.append("no data retrieved")
        return result


def _count_csv_rows(text: str) -> int:
    """Number of data rows (excludes header)."""
    try:
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
    except csv.Error:
        return max(0, text.count("\n") - 1)
    if not rows:
        return 0
    # Socrata exports always include a header row
    return len(rows) - 1
