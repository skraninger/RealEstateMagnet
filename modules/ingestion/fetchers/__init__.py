"""Fetcher registry: protocol → fetcher class."""

from __future__ import annotations

from .arcgis_fetcher import ArcGisFetcher
from .base import BaseFetcher, DatasetInfo, FetchResult
from .census_fetcher import CensusFetcher
from .ckan_fetcher import CkanFetcher
from .direct_fetcher import DirectFetcher
from .socrata_fetcher import SocrataFetcher

FETCHER_REGISTRY: dict[str, type[BaseFetcher]] = {
    "socrata": SocrataFetcher,
    "arcgis": ArcGisFetcher,
    "ckan": CkanFetcher,
    "census": CensusFetcher,
    "direct": DirectFetcher,
}

__all__ = [
    "FETCHER_REGISTRY",
    "BaseFetcher",
    "DatasetInfo",
    "FetchResult",
    "ArcGisFetcher",
    "CensusFetcher",
    "CkanFetcher",
    "DirectFetcher",
    "SocrataFetcher",
]
