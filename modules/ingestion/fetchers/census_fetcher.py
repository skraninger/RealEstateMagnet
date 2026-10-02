"""
Census API (ACS) fetcher — demographic/housing context for livability scoring.

The source registry lists Census as protocol="direct" with base_url pointing
at a dataset endpoint, e.g. https://api.census.gov/data/2022/acs/acs5.
This fetcher turns that into a small set of real-estate-relevant tables:

  GET {base}?get=NAME,{var}&for=state:12&in=county:*&key={CENSUS_API_KEY}

Returns JSON ``[header, row, row, ...]`` which is converted to records.
"""

from __future__ import annotations

import logging
import os

from ...discovery.florida_sources import FloridaSource
from .base import BaseFetcher, DatasetInfo, FetchResult

logger = logging.getLogger(__name__)

# Real-estate / livability-relevant ACS 5-year variables (MVP shortlist)
DEFAULT_VARIABLES: dict[str, str] = {
    "B25001": "Housing occupancy",
    "B25035": "Median selected monthly costs",
    "B25077": "Median value of owner-occupied housing units",
    "B01003": "Total population",
    "B09001": "Housing units by occupancy",
}

FL_STATE_FIPS = "12"


class CensusFetcher(BaseFetcher):
    protocol = "census"

    def __init__(self, client, variables: dict[str, str] | None = None) -> None:
        super().__init__(client)
        self.variables = variables or DEFAULT_VARIABLES

    async def discover(self, source: FloridaSource) -> list[DatasetInfo]:
        base = source.base_url.rstrip("/")
        return [
            DatasetInfo(
                dataset_id=var,
                name=label,
                url=f"{base}?get=NAME,{var}&for=state:{FL_STATE_FIPS}&in=county:*",
                format_hint="json",
                metadata={"variable": var},
            )
            for var, label in self.variables.items()
        ]

    async def fetch(
        self, source: FloridaSource, dataset: DatasetInfo
    ) -> FetchResult:
        base = source.base_url.rstrip("/")
        result = FetchResult(
            source_name=source.name,
            dataset_id=dataset.dataset_id,
            format="json",
            url=dataset.url or f"{base}?get=NAME,{dataset.dataset_id}&for=state:{FL_STATE_FIPS}&in=county:*",
        )

        params: dict[str, str] = {
            "get": f"NAME,{dataset.dataset_id}",
            "for": f"state:{FL_STATE_FIPS}",
            "in": "county:*",
        }
        api_key = os.environ.get("CENSUS_API_KEY", "").strip()
        if api_key:
            params["key"] = api_key

        try:
            resp = await self.client.get(base, params=params)
        except Exception as exc:
            result.warnings.append(f"census request failed: {exc}")
            return result

        if resp.status_code != 200:
            result.warnings.append(f"census API -> HTTP {resp.status_code}: {resp.text[:200]}")
            return result

        try:
            payload = resp.json()
        except ValueError:
            result.warnings.append("census API returned non-JSON")
            return result

        if not isinstance(payload, list) or len(payload) < 2:
            result.warnings.append("unexpected census payload shape")
            return result

        header = payload[0]
        records = [dict(zip(header, row)) for row in payload[1:]]
        result.records = records
        result.row_count = len(records)
        return result
