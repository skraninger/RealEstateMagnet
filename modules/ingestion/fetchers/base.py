"""
FetchResult / BaseFetcher — common contract for all ingestion fetchers.

A fetcher knows how to (a) discover datasets on a source and (b) download
one dataset as verbatim bytes or parsed records.  Normalisation into the
Master Schema is Phase 3 — fetchers must not couple to it.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from ...discovery.florida_sources import FloridaSource
from ..polite_client import PoliteClient


@dataclass
class DatasetInfo:
    """A single harvestable dataset discovered on a source."""

    dataset_id: str
    name: str
    url: str = ""                 # direct download/export URL when known
    format_hint: str = "json"     # csv | json | geojson | zip | shp | xlsx | html
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchResult:
    """Outcome of fetching one dataset."""

    source_name: str
    dataset_id: str
    format: str                   # csv|json|geojson|html|zip|shp|pdf|xlsx|unknown
    records: list[dict] | None = None      # for API/table data
    raw_bytes: bytes | None = None         # for file downloads
    url: str = ""
    row_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def size_bytes(self) -> int:
        if self.raw_bytes is not None:
            return len(self.raw_bytes)
        if self.records is not None:
            import json as _json
            return len(_json.dumps(self.records))
        return 0


class BaseFetcher(abc.ABC):
    """
    Contract for protocol-specific fetchers.

    Subclasses set ``protocol`` and implement :meth:`discover` and
    :meth:`fetch`.  All HTTP goes through the shared :class:`PoliteClient`.
    """

    protocol: str = "base"

    def __init__(self, client: PoliteClient) -> None:
        self.client = client

    @abc.abstractmethod
    async def discover(self, source: FloridaSource) -> list[DatasetInfo]:
        """List the datasets available on ``source``."""

    @abc.abstractmethod
    async def fetch(
        self, source: FloridaSource, dataset: DatasetInfo
    ) -> FetchResult:
        """Download one dataset; never raise for HTTP-level failures —
        report them via ``FetchResult.warnings`` instead."""
