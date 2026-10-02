"""
ArcGIS REST fetcher — county GIS portals, FGDL, FEMA NFHL, ...

Discovery:
  • base_url pointing at a service (.../MapServer|FeatureServer):
      GET {base} → layers[]
  • base_url pointing at an ArcGIS server root (e.g. https://host/arcgis/rest/services):
      GET {base} → services[] → per-service layer walk
  • ArcGIS Online org page (https://{org}.opendata.arcgis.com):
      probed via /server/rest/services, best-effort

Fetch:
  Feature layers → paged ``query?where=1=1&outFields=*&f=json``
  (default page size 2000, configurable); attributes only.
  Non-feature layers (tiles/imagery) are skipped with a warning —
  tile export is out of scope for the MVP.
"""

from __future__ import annotations

import logging

from ...discovery.florida_sources import FloridaSource
from .base import BaseFetcher, DatasetInfo, FetchResult

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 2000
SERVICE_ROOT_HINTS = ("/rest/services", "/arcgis/rest/services")


class ArcGisFetcher(BaseFetcher):
    protocol = "arcgis"

    def __init__(self, client, page_size: int = DEFAULT_PAGE_SIZE) -> None:
        super().__init__(client)
        self.page_size = page_size

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def discover(self, source: FloridaSource) -> list[DatasetInfo]:
        base = source.base_url.rstrip("/")
        lower = base.lower()

        if lower.endswith(("/mapserver", "/featureserver")):
            return await self._layers_of_service(base, source.name)

        # Try server-root style discovery (host/arcgis/rest/services or /rest/services)
        for candidate in self._service_root_candidates(base):
            datasets = await self._walk_service_tree(candidate, source.name)
            if datasets:
                return datasets

        # ArcGIS Online org page fallback
        org_root = f"{base}/server/rest/services"
        datasets = await self._walk_service_tree(org_root, source.name)
        if datasets:
            return datasets

        logger.warning("ArcGIS discovery found nothing for %s", base)
        return []

    @staticmethod
    def _service_root_candidates(base: str) -> list[str]:
        if any(hint in base.lower() for hint in SERVICE_ROOT_HINTS):
            return [base]
        # insert /arcgis/rest/services under the host
        from urllib.parse import urlparse, urlunparse
        p = urlparse(base)
        path = p.path.rstrip("/")
        # host-only URLs (https://host/) → conventional ArcGIS server root
        return [urlunparse((p.scheme, p.netloc, f"{path}/arcgis/rest/services", "", "", ""))]

    async def _walk_service_tree(self, root: str, source_name: str) -> list[DatasetInfo]:
        try:
            resp = await self.client.get(root)
        except Exception as exc:
            logger.debug("ArcGIS service tree %s failed: %s", root, exc)
            return []
        if resp.status_code != 200:
            return []
        try:
            payload = resp.json()
        except ValueError:
            return []

        services = payload.get("services")
        if not isinstance(services, list):
            return []

        datasets: list[DatasetInfo] = []
        for svc in services[:200]:   # bounded walk to stay polite
            name, stype = svc.get("name"), svc.get("type", "")
            if stype not in ("MapServer", "FeatureServer"):
                continue
            svc_url = f"{root}/{name}/{stype}"
            layers = await self._layers_of_service(svc_url, source_name)
            datasets.extend(layers)
        return datasets

    async def _layers_of_service(self, service_url: str, source_name: str) -> list[DatasetInfo]:
        try:
            resp = await self.client.get(service_url)
        except Exception as exc:
            logger.debug("ArcGIS service %s failed: %s", service_url, exc)
            return []
        if resp.status_code != 200:
            return []
        try:
            payload = resp.json()
        except ValueError:
            return []

        datasets: list[DatasetInfo] = []
        for layer in payload.get("layers", []):
            lid, lname = layer.get("id"), layer.get("name", "")
            if not isinstance(lid, int) or not lname:
                continue
            # skip boundary/extent pseudo-layers
            if lname.lower() in ("boundary and extent", "boundary and extents"):
                continue
            datasets.append(DatasetInfo(
                dataset_id=f"{lid}:{lname}",
                name=lname,
                url=f"{service_url}/{lid}",
                format_hint="geojson",
                metadata={"service": service_url},
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
            format="geojson",
            url=dataset.url,
        )

        features: list[dict] = []
        offset = 0
        try:
            while True:
                resp = await self.client.get(
                    f"{dataset.url}/query",
                    params={
                        "where": "1=1",
                        "outFields": "*",
                        "f": "json",
                        "resultOffset": offset,
                        "numRecords": self.page_size,
                    },
                )
                if resp.status_code != 200:
                    result.warnings.append(f"query at offset {offset} -> HTTP {resp.status_code}")
                    break
                payload = resp.json()
                page_features = payload.get("features", [])
                features.extend(
                    f.get("attributes", {}) for f in page_features
                )
                if not payload.get("exceededTransferLimit", False) or not page_features:
                    break
                offset += self.page_size
        except Exception as exc:
            result.warnings.append(f"query aborted: {exc}")

        result.records = features or None
        result.row_count = len(features)
        if not features:
            result.warnings.append("no features returned (layer may be imagery/tiles)")
        return result
