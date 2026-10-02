"""
Tests for Phase 2 — Ingestion & Extraction module.

All tests run offline: HTTP is mocked with httpx.MockTransport injected via
PoliteClient(transport=...), and the rate limiter uses a fake clock/sleep.
The single Playwright test renders a data: URL (no network) and skips when
the chromium browser is not installed.

Run with:  pytest tests/test_ingestion.py -v
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable
from unittest import mock

import httpx
import pytest
import pytest_asyncio

from modules.discovery.florida_sources import FloridaSource
from modules.discovery.web_crawler import CrawlResult, DataSignal, SignalType
from modules.ingestion.polite_client import (
    PoliteClient,
    ProxyRotatedError,
    RetryableHTTPError,
    _parse_retry_after,
)
from modules.ingestion.fetchers.base import DatasetInfo, FetchResult
from modules.ingestion.fetchers.socrata_fetcher import SocrataFetcher
from modules.ingestion.fetchers.arcgis_fetcher import ArcGisFetcher
from modules.ingestion.fetchers.ckan_fetcher import CkanFetcher
from modules.ingestion.fetchers.census_fetcher import CensusFetcher
from modules.ingestion.fetchers.direct_fetcher import DirectFetcher
from modules.ingestion.fetchers.web_fetcher import (
    WebFetcher,
    _extract_embedded_json,
    _extract_table_records,
)
from modules.ingestion.storage import RawStore, extension_for, slugify
from modules.ingestion.ingestion_engine import IngestionEngine, find_sources

FIXTURES = Path(__file__).parent / "fixtures" / "ingestion"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_client(handler: Callable[[httpx.Request], httpx.Response], **kwargs) -> PoliteClient:
    """PoliteClient with a mock transport and no throttling."""
    kwargs.setdefault("min_interval", 0.0)
    kwargs.setdefault("jitter", 0.0)
    return PoliteClient(transport=httpx.MockTransport(handler), **kwargs)


def make_source(
    name: str = "Test Source",
    base_url: str = "https://opendata.test.gov",
    protocol: str = "socrata",
    **kw: Any,
) -> FloridaSource:
    return FloridaSource(
        name=name, base_url=base_url, protocol=protocol,  # type: ignore[arg-type]
        categories=["property"], **kw,
    )


def html_response(path: Path) -> httpx.Response:
    return httpx.Response(200, content=path.read_bytes(), headers={"content-type": "text/html"})


# ---------------------------------------------------------------------------
# M1 — PoliteClient: rate limiting
# ---------------------------------------------------------------------------

class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_per_host_interval_enforced(self):
        clock_now = [1000.0]
        sleeps: list[float] = []

        def clock() -> float:
            return clock_now[0]

        async def fake_sleep(s: float) -> None:
            sleeps.append(s)
            clock_now[0] += s

        client = PoliteClient(
            min_interval=1.5, jitter=0.25,
            transport=httpx.MockTransport(lambda r: httpx.Response(200)),
            clock=clock, sleep_func=fake_sleep,
        )
        await client.get("https://a.test.gov/x")   # first hit: no wait
        await client.get("https://a.test.gov/y")   # second hit: waits ~1.5–1.75s
        assert len(sleeps) == 1
        assert 1.5 <= sleeps[0] <= 1.75 + 1e-9

    @pytest.mark.asyncio
    async def test_different_hosts_do_not_throttle_each_other(self):
        sleeps: list[float] = []
        async def fake_sleep(s: float) -> None:
            sleeps.append(s)
        client = PoliteClient(
            min_interval=1.5, jitter=0.0,
            transport=httpx.MockTransport(lambda r: httpx.Response(200)),
            clock=lambda: 1000.0, sleep_func=fake_sleep,
        )
        await client.get("https://a.test.gov/x")
        await client.get("https://b.test.gov/x")   # different host → no wait
        assert sleeps == []

    @pytest.mark.asyncio
    async def test_no_wait_when_interval_elapsed(self):
        clock_now = [1000.0]
        sleeps: list[float] = []
        async def fake_sleep(s: float) -> None:
            sleeps.append(s)
        client = PoliteClient(
            min_interval=1.5, jitter=0.0,
            transport=httpx.MockTransport(lambda r: httpx.Response(200)),
            clock=lambda: clock_now[0], sleep_func=fake_sleep,
        )
        await client.get("https://a.test.gov/x")
        clock_now[0] += 10.0                        # plenty of time passed
        await client.get("https://a.test.gov/y")
        assert sleeps == []


class TestPoliteClientConfig:
    def test_min_interval_from_env(self, monkeypatch):
        monkeypatch.setenv("CRAWL_DELAY_SECONDS", "2.5")
        client = PoliteClient()
        assert client.min_interval == 2.5

    def test_proxy_list_from_env(self, monkeypatch):
        monkeypatch.setenv("PROXY_LIST", "http://p1:8080, http://p2:8080")
        client = PoliteClient()
        assert client.proxy_list == ["http://p1:8080", "http://p2:8080"]

    def test_proxy_list_explicit_wins(self, monkeypatch):
        monkeypatch.setenv("PROXY_LIST", "http://env:8080")
        client = PoliteClient(proxy_list=["http://explicit:8080"])
        assert client.proxy_list == ["http://explicit:8080"]

    def test_no_proxy_by_default(self, monkeypatch):
        for var in ("PROXY_LIST", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            monkeypatch.delenv(var, raising=False)
        client = PoliteClient()
        assert client.proxy_list == []

    def test_parse_retry_after(self):
        assert _parse_retry_after("3") == 3.0
        assert _parse_retry_after(None) is None
        assert _parse_retry_after("not-a-number") is None
        assert _parse_retry_after("-5") == 0.0


# ---------------------------------------------------------------------------
# M1 — PoliteClient: retries & proxy rotation
# ---------------------------------------------------------------------------

class TestRetries:
    @pytest.mark.asyncio
    async def test_429_retries_then_succeeds(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
            return httpx.Response(200, text="ok")

        client = make_client(handler, max_attempts=3, base_delay=0.01)
        resp = await client.get("https://a.test.gov/x")
        assert resp.status_code == 200
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_5xx_exhausts_attempts_and_raises(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(503, text="unavailable")

        client = make_client(handler, max_attempts=3, base_delay=0.01)
        with pytest.raises(RetryableHTTPError) as exc_info:
            await client.get("https://a.test.gov/x")
        assert exc_info.value.status_code == 503
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_4xx_not_retried(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(404, text="nope")

        client = make_client(handler, max_attempts=3)
        resp = await client.get("https://a.test.gov/x")
        assert resp.status_code == 404
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_get_bytes_returns_body(self):
        client = make_client(lambda r: httpx.Response(200, content=b"raw-bytes"))
        assert await client.get_bytes("https://a.test.gov/f.csv") == b"raw-bytes"


class TestProxyRotation:
    @pytest.mark.asyncio
    async def test_rotation_on_connection_failure(self):
        # .invalid TLD guarantees fast DNS failure for both proxies
        client = PoliteClient(
            min_interval=0.0, jitter=0.0,
            proxy_list=["http://proxy-a.invalid:8080", "http://proxy-b.invalid:8080"],
            max_attempts=3, base_delay=0.01,
        )
        with pytest.raises((ProxyRotatedError, httpx.TransportError)):
            await client.get("https://target.test.gov/x")
        state = client._hosts["target.test.gov"]
        assert state.proxy_index >= 2   # rotated past both proxies
        assert state.failures >= 2

    def test_proxy_for_cycles_round_robin(self):
        client = PoliteClient(proxy_list=["http://p1", "http://p2"])
        state = client._host_state("h")
        assert client._proxy_for(state) == "http://p1"
        state.proxy_index += 1
        assert client._proxy_for(state) == "http://p2"
        state.proxy_index += 1
        assert client._proxy_for(state) == "http://p1"

    def test_proxy_for_none_without_pool(self):
        client = PoliteClient(proxy_list=[])
        assert client._proxy_for(client._host_state("h")) is None

    @pytest.mark.asyncio
    async def test_check_proxies_reports_healthy_subset(self):
        client = PoliteClient(min_interval=0.0, proxy_list=["http://a.test", "http://b.test"])
        ok_transport = httpx.MockTransport(lambda r: httpx.Response(204))

        def failing(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("proxy down")

        client._clients["http://a.test"] = httpx.AsyncClient(transport=ok_transport)
        client._clients["http://b.test"] = httpx.AsyncClient(transport=httpx.MockTransport(failing))
        healthy = await client.check_proxies()
        assert healthy == ["http://a.test"]


# ---------------------------------------------------------------------------
# M2 — Socrata fetcher
# ---------------------------------------------------------------------------

SOCRATA_CATALOG = [
    {"id": "m39i-ikge", "name": "Property Assessor Parcel Data", "category": "Property", "rowsUpdatedAt": 1700000000},
    {"id": "abc-defg", "name": "Building Permits", "category": "Permits", "rowsUpdatedAt": 1700000001},
]


class TestSocrataFetcher:
    @pytest.mark.asyncio
    async def test_discover_parses_catalog(self):
        client = make_client(lambda r: httpx.Response(200, json=SOCRATA_CATALOG))
        fetcher = SocrataFetcher(client)
        datasets = await fetcher.discover(make_source())
        assert len(datasets) == 2
        assert datasets[0].dataset_id == "m39i-ikge"
        assert datasets[0].name == "Property Assessor Parcel Data"
        assert datasets[0].url.endswith("/api/views/m39i-ikge/rows.csv?$limit=-1")
        assert datasets[0].format_hint == "csv"

    @pytest.mark.asyncio
    async def test_discover_empty_on_error(self):
        client = make_client(lambda r: httpx.Response(500))
        fetcher = SocrataFetcher(client)
        assert await fetcher.discover(make_source()) == []

    @pytest.mark.asyncio
    async def test_fetch_full_export_csv(self):
        csv_body = "parcel,owner,tax\nP1,Smith,100\nP2,Jones,200\n"
        client = make_client(
            lambda r: httpx.Response(200, content=csv_body.encode(),
                                     headers={"content-type": "text/csv"})
        )
        fetcher = SocrataFetcher(client)
        ds = DatasetInfo(dataset_id="m39i-ikge", name="Parcels",
                         url="https://opendata.test.gov/api/views/m39i-ikge/rows.csv?$limit=-1")
        result = await fetcher.fetch(make_source(), ds)
        assert result.raw_bytes == csv_body.encode()
        assert result.row_count == 2
        assert result.format == "csv"
        assert result.warnings == []

    @pytest.mark.asyncio
    async def test_fetch_paged_fallback_strips_headers(self, monkeypatch):
        import modules.ingestion.fetchers.socrata_fetcher as mod
        monkeypatch.setattr(mod, "PAGE_SIZE", 2)

        page1 = "a,b\n1,x\n2,y\n"          # full page (header + 2 rows)
        page2 = "a,b\n3,z\n"               # partial page (header + 1 row)
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "limit=-1" in url:   # httpx encodes $ as %24
                return httpx.Response(403, text="full export disabled")
            calls.append(request.url.params.get("$offset", "0"))
            body = page1 if not calls or calls[-1] == "0" else page2
            return httpx.Response(200, content=body.encode(),
                                  headers={"content-type": "text/csv"})

        client = make_client(handler)
        fetcher = SocrataFetcher(client)
        ds = DatasetInfo(dataset_id="m39i-ikge", name="Parcels")
        result = await fetcher.fetch(make_source(), ds)
        assert result.row_count == 3
        assert result.raw_bytes.count(b"a,b") == 1   # header deduplicated
        assert any("falling back to paging" in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_fetch_counts_csv_rows_without_header(self):
        from modules.ingestion.fetchers.socrata_fetcher import _count_csv_rows
        assert _count_csv_rows("h1,h2\nr1,r2\n") == 1
        assert _count_csv_rows("") == 0


# ---------------------------------------------------------------------------
# M2 — ArcGIS fetcher
# ---------------------------------------------------------------------------

class TestArcGisFetcher:
    @pytest.mark.asyncio
    async def test_discover_from_service_url(self):
        service = {"layers": [
            {"id": 0, "name": "Boundary and Extent"},
            {"id": 1, "name": "Parcels"},
            {"id": 2, "name": "Zoning"},
        ]}
        client = make_client(lambda r: httpx.Response(200, json=service))
        fetcher = ArcGisFetcher(client)
        source = make_source(base_url="https://gis.test.gov/arcgis/rest/services/TaxMap/MapServer", protocol="arcgis")
        datasets = await fetcher.discover(source)
        names = [d.name for d in datasets]
        assert "Boundary and Extent" not in names
        assert names == ["Parcels", "Zoning"]
        assert datasets[0].url.endswith("/TaxMap/MapServer/1")

    @pytest.mark.asyncio
    async def test_discover_walks_service_tree(self):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/arcgis/rest/services"):
                return httpx.Response(200, json={"services": [
                    {"name": "TaxMap.Parcels", "type": "FeatureServer"},
                    {"name": "Aerial", "type": "ImageServer"},   # skipped type
                ]})
            if url.endswith("/TaxMap.Parcels/FeatureServer"):
                return httpx.Response(200, json={"layers": [{"id": 3, "name": "Parcels"}]})
            return httpx.Response(404)

        client = make_client(handler)
        fetcher = ArcGisFetcher(client)
        source = make_source(base_url="https://gis.test.gov", protocol="arcgis")
        datasets = await fetcher.discover(source)
        assert len(datasets) == 1
        assert datasets[0].name == "Parcels"
        assert datasets[0].url.endswith("/TaxMap.Parcels/FeatureServer/3")

    @pytest.mark.asyncio
    async def test_discover_finds_nothing(self):
        client = make_client(lambda r: httpx.Response(404))
        fetcher = ArcGisFetcher(client)
        source = make_source(base_url="https://dead.test.gov", protocol="arcgis")
        assert await fetcher.discover(source) == []

    @pytest.mark.asyncio
    async def test_fetch_pages_feature_layer(self):
        def handler(request: httpx.Request) -> httpx.Response:
            offset = int(request.url.params.get("resultOffset", "0"))
            if offset == 0:
                return httpx.Response(200, json={
                    "features": [{"attributes": {"ID": 1}}, {"attributes": {"ID": 2}}],
                    "exceededTransferLimit": True,
                })
            return httpx.Response(200, json={
                "features": [{"attributes": {"ID": 3}}],
                "exceededTransferLimit": False,
            })

        client = make_client(handler)
        fetcher = ArcGisFetcher(client, page_size=2)
        ds = DatasetInfo(dataset_id="1:Parcels", name="Parcels",
                         url="https://gis.test.gov/svc/FeatureServer/1")
        result = await fetcher.fetch(make_source(protocol="arcgis"), ds)
        assert result.row_count == 3
        assert [r["ID"] for r in result.records] == [1, 2, 3]
        assert result.format == "geojson"

    @pytest.mark.asyncio
    async def test_fetch_error_becomes_warning(self):
        client = make_client(lambda r: httpx.Response(404))
        fetcher = ArcGisFetcher(client)
        ds = DatasetInfo(dataset_id="1:X", name="X", url="https://gis.test.gov/svc/FeatureServer/1")
        result = await fetcher.fetch(make_source(protocol="arcgis"), ds)
        assert result.records is None
        assert any("HTTP 404" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# M2 — CKAN fetcher
# ---------------------------------------------------------------------------

class TestCkanFetcher:
    @pytest.mark.asyncio
    async def test_discover_package_list(self):
        client = make_client(lambda r: httpx.Response(200, json={"result": ["pkg-a", "pkg-b"]}))
        fetcher = CkanFetcher(client)
        datasets = await fetcher.discover(make_source(protocol="ckan"))
        assert [d.dataset_id for d in datasets] == ["pkg-a", "pkg-b"]

    @pytest.mark.asyncio
    async def test_fetch_downloads_preferred_csv_resource(self):
        pkg = {"result": {
            "title": "Parcels",
            "resources": [
                {"format": "API", "url": "https://ckan.test.gov/api/rows"},
                {"format": "CSV", "url": "https://ckan.test.gov/data/parcels.csv"},
            ],
        }}
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "package_show" in url:
                return httpx.Response(200, json=pkg)
            return httpx.Response(200, content=b"id,name\n1,a\n2,b\n")

        client = make_client(handler)
        fetcher = CkanFetcher(client)
        ds = DatasetInfo(dataset_id="pkg-a", name="pkg-a")
        result = await fetcher.fetch(make_source(protocol="ckan"), ds)
        assert result.format == "csv"
        assert result.row_count == 2
        assert result.raw_bytes is not None
        assert result.metadata["package_title"] == "Parcels"

    @pytest.mark.asyncio
    async def test_fetch_no_resources_warns(self):
        client = make_client(
            lambda r: httpx.Response(200, json={"result": {"title": "Empty", "resources": []}})
        )
        fetcher = CkanFetcher(client)
        result = await fetcher.fetch(make_source(protocol="ckan"), DatasetInfo(dataset_id="p", name="p"))
        assert result.raw_bytes is None
        assert any("no downloadable" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# M2 — Census fetcher
# ---------------------------------------------------------------------------

class TestCensusFetcher:
    @pytest.mark.asyncio
    async def test_discover_returns_default_variables(self, monkeypatch):
        monkeypatch.delenv("CENSUS_API_KEY", raising=False)
        client = make_client(lambda r: httpx.Response(200))
        fetcher = CensusFetcher(client)
        datasets = await fetcher.discover(make_source(protocol="direct"))
        ids = {d.dataset_id for d in datasets}
        assert "B25077" in ids and "B01003" in ids

    @pytest.mark.asyncio
    async def test_fetch_converts_payload_to_records(self, monkeypatch):
        monkeypatch.delenv("CENSUS_API_KEY", raising=False)
        payload = [
            ["NAME", "B25077E", "STATE", "COUNTY"],
            ["Median value of owner-occupied housing units", "250000", "12", "001"],
            ["Median value of owner-occupied housing units", "310000", "12", "011"],
        ]
        client = make_client(lambda r: httpx.Response(200, json=payload))
        fetcher = CensusFetcher(client)
        ds = DatasetInfo(dataset_id="B25077", name="Median value")
        result = await fetcher.fetch(make_source(protocol="direct"), ds)
        assert result.row_count == 2
        assert result.records[0]["B25077E"] == "250000"
        assert result.records[1]["COUNTY"] == "011"

    @pytest.mark.asyncio
    async def test_fetch_error_becomes_warning(self):
        client = make_client(lambda r: httpx.Response(403, text="key required"))
        fetcher = CensusFetcher(client)
        result = await fetcher.fetch(make_source(protocol="direct"),
                                     DatasetInfo(dataset_id="B25077", name="x"))
        assert result.records is None
        assert any("HTTP 403" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# M3 — Direct fetcher
# ---------------------------------------------------------------------------

class TestDirectFetcher:
    @pytest.mark.asyncio
    async def test_discover_single_file_url(self):
        client = make_client(lambda r: httpx.Response(200))
        fetcher = DirectFetcher(client)
        source = make_source(base_url="https://pa.test.gov/files/parcel_2026.csv", protocol="direct")
        datasets = await fetcher.discover(source)
        assert len(datasets) == 1
        assert datasets[0].url == "https://pa.test.gov/files/parcel_2026.csv"
        assert datasets[0].format_hint == "csv"

    @pytest.mark.asyncio
    async def test_discover_html_page_extracts_same_host_links(self):
        client = make_client(lambda r: html_response(FIXTURES / "links_page.html"))
        fetcher = DirectFetcher(client)
        source = make_source(base_url="https://pa.test.gov/data", protocol="direct")
        datasets = await fetcher.discover(source)
        urls = {d.url for d in datasets}
        assert "https://pa.test.gov/downloads/nal_2026.zip" in urls
        assert "https://pa.test.gov/downloads/assessed_values.csv" in urls
        # non-data and external links excluded
        assert not any("logo.png" in u for u in urls)
        assert not any("other.example.com" in u for u in urls)

    @pytest.mark.asyncio
    async def test_fetch_404_is_warning_not_crash(self):
        client = make_client(lambda r: httpx.Response(404))
        fetcher = DirectFetcher(client)
        ds = DatasetInfo(dataset_id="f.csv", name="f", url="https://pa.test.gov/f.csv")
        result = await fetcher.fetch(make_source(protocol="direct"), ds)
        assert result.raw_bytes is None
        assert any("404" in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_fetch_records_bytes_and_declared_size(self):
        body = b"a,b\n1,2\n"
        client = make_client(
            lambda r: httpx.Response(200, content=body, headers={"content-length": str(len(body))})
        )
        fetcher = DirectFetcher(client)
        ds = DatasetInfo(dataset_id="f.csv", name="f", url="https://pa.test.gov/f.csv")
        result = await fetcher.fetch(make_source(protocol="direct"), ds)
        assert result.raw_bytes == body
        assert result.metadata["declared_size"] == len(body)
        assert result.row_count == 1


# ---------------------------------------------------------------------------
# M5 — Storage
# ---------------------------------------------------------------------------

class TestStorage:
    def test_slugify(self):
        assert slugify("Broward County Open Data") == "broward-county-open-data"
        assert slugify("") == "unnamed"
        assert slugify("a/b\\c:d e") == "a-b-c-d-e"
        assert len(slugify("x" * 200)) <= 60

    def test_extension_for(self):
        assert extension_for("geojson") == "json"
        assert extension_for("csv") == "csv"
        assert extension_for("weird") == "bin"

    def test_write_records_and_manifest(self, tmp_path):
        store = RawStore(tmp_path)
        result = FetchResult(source_name="Test Source", dataset_id="d-1", format="json",
                             records=[{"a": 1}], url="https://x/y")
        path = store.write(result)
        assert path.exists() and path.suffix == ".json"
        payload = json.dumps(result.records, indent=1).encode("utf-8")
        entry = store.manifest[-1]
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
        assert entry["size_bytes"] == len(payload)
        assert entry["source"] == "Test Source"
        assert store.has_entry("Test Source", "d-1")

    def test_write_replaces_existing_entry(self, tmp_path):
        store = RawStore(tmp_path)
        r1 = FetchResult(source_name="S", dataset_id="d", format="csv", raw_bytes=b"old")
        store.write(r1)
        r2 = FetchResult(source_name="S", dataset_id="d", format="csv", raw_bytes=b"new-data")
        store.write(r2)
        assert len(store.manifest) == 1
        assert store.manifest[0]["size_bytes"] == len(b"new-data")

    def test_manifest_persistence(self, tmp_path):
        store = RawStore(tmp_path)
        store.write(FetchResult(source_name="S", dataset_id="d", format="csv", raw_bytes=b"x"))
        reloaded = RawStore(tmp_path)
        assert reloaded.has_entry("S", "d")


# ---------------------------------------------------------------------------
# M5 — Engine
# ---------------------------------------------------------------------------

def _socrata_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "/api/views" in url and "rows.csv" not in url:
        return httpx.Response(200, json=SOCRATA_CATALOG)
    return httpx.Response(200, content=b"id,name\n1,a\n", headers={"content-type": "text/csv"})


class TestIngestionEngine:
    @pytest.mark.asyncio
    async def test_dry_run_makes_no_downloads(self, tmp_path):
        calls = []
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return _socrata_handler(request)

        client = make_client(handler)
        engine = IngestionEngine(client=client, store_root=tmp_path)
        report = await engine.ingest(make_source(), dry_run=True)
        assert report.dry_run is True
        assert report.datasets_discovered == 2
        assert len(report.planned_datasets) == 2
        assert report.datasets_fetched == 0
        # only the catalog call hit the network
        assert all("rows.csv" not in c for c in calls)

    @pytest.mark.asyncio
    async def test_ingest_with_limit(self, tmp_path):
        client = make_client(_socrata_handler)
        engine = IngestionEngine(client=client, store_root=tmp_path, limit=1)
        report = await engine.ingest(make_source())
        assert report.datasets_fetched == 1
        assert report.total_rows == 1
        assert (tmp_path / "test-source" / "m39i-ikge.csv").exists()

    @pytest.mark.asyncio
    async def test_rerun_skips_manifested_datasets(self, tmp_path):
        client = make_client(_socrata_handler)
        source = make_source()
        first = IngestionEngine(client=client, store_root=tmp_path)
        r1 = await first.ingest(source)
        assert r1.datasets_fetched == 2

        second = IngestionEngine(client=client, store_root=tmp_path)
        r2 = await second.ingest(source)
        assert r2.datasets_fetched == 0
        assert r2.datasets_skipped == 2

    @pytest.mark.asyncio
    async def test_force_refetches(self, tmp_path):
        client = make_client(_socrata_handler)
        source = make_source()
        await IngestionEngine(client=client, store_root=tmp_path).ingest(source)
        engine = IngestionEngine(client=client, store_root=tmp_path, force=True)
        report = await engine.ingest(source)
        assert report.datasets_fetched == 2

    @pytest.mark.asyncio
    async def test_unknown_protocol_reported(self, tmp_path):
        client = make_client(lambda r: httpx.Response(200))
        engine = IngestionEngine(client=client, store_root=tmp_path)
        source = FloridaSource(name="Odd", base_url="ftp://x", protocol="ftp",  # type: ignore[arg-type]
                               categories=["misc"])
        report = await engine.ingest(source)
        assert any("no fetcher" in e for e in report.errors)

    def test_find_sources_filters(self):
        broward = find_sources(name="Broward")
        assert len(broward) >= 1 and all("broward" in s.name.lower() for s in broward)
        socrata = find_sources(protocol="socrata")
        assert len(socrata) >= 2 and all(s.protocol == "socrata" for s in socrata)
        tax_socrata = find_sources(category="tax", protocol="socrata")
        assert all("tax" in s.categories for s in tax_socrata)
        assert find_sources(name="no-such-source-xyz") == []


# ---------------------------------------------------------------------------
# M4 — Web fetcher
# ---------------------------------------------------------------------------

class FakeCrawler:
    def __init__(self, result: CrawlResult):
        self._result = result
        self.calls: list[tuple[str, dict | None]] = []

    async def crawl(self, source: FloridaSource, auth_cookies: dict | None = None) -> CrawlResult:
        self.calls.append((source.name, auth_cookies))
        return self._result


def _signal_result() -> CrawlResult:
    signals = [
        DataSignal(url="https://site.test.gov/downloads/parcel.csv",
                   signal_type=SignalType.DOWNLOAD_LINK, format_hint="csv", relevance_score=0.95,
                   anchor_text="Parcel CSV"),
        DataSignal(url="https://site.test.gov/data",
                   signal_type=SignalType.HTML_TABLE, format_hint="html_table", relevance_score=0.8,
                   anchor_text="Parcel ID Owner Tax", table_row_count=4, table_col_count=3),
        DataSignal(url="https://site.test.gov/data",
                   signal_type=SignalType.JSON_BLOB, format_hint="json", relevance_score=0.65),
    ]
    return CrawlResult(seed_url="https://site.test.gov", source_name="Site", pages_visited=1,
                       signals=signals, gated_urls=[], robots_blocked=[], errors=[])


class TestWebFetcherDiscover:
    @pytest.mark.asyncio
    async def test_signals_become_datasets(self):
        client = make_client(lambda r: httpx.Response(200))
        fake = FakeCrawler(_signal_result())
        fetcher = WebFetcher(client, crawler=fake)
        datasets = await fetcher.discover(make_source(base_url="https://site.test.gov", protocol="web"))
        kinds = [d.metadata.get("signal") for d in datasets]
        assert "download_link" in kinds and "html_table" in kinds and "json_blob" in kinds
        link = next(d for d in datasets if d.metadata.get("signal") == "download_link")
        assert link.url.endswith("parcel.csv")

    @pytest.mark.asyncio
    async def test_gated_source_emits_auth_placeholder(self):
        client = make_client(lambda r: httpx.Response(200))
        fake = FakeCrawler(_signal_result())
        fetcher = WebFetcher(client, crawler=fake)
        source = make_source(base_url="https://gated.test.gov", protocol="web",
                             requires_auth=True, auth_notes="MLS login required")
        datasets = await fetcher.discover(source)
        assert len(datasets) == 1
        assert datasets[0].dataset_id == "__auth_required__"
        assert fake.calls == []   # no crawl attempted without credentials

    @pytest.mark.asyncio
    async def test_auth_cookies_forwarded_to_crawler(self):
        client = make_client(lambda r: httpx.Response(200))
        fake = FakeCrawler(_signal_result())
        fetcher = WebFetcher(client, crawler=fake, auth_cookies={"SMLSSID": "abc"})
        source = make_source(base_url="https://gated.test.gov", protocol="web", requires_auth=True)
        await fetcher.discover(source)
        assert fake.calls == [("Test Source", {"SMLSSID": "abc"})]

    @pytest.mark.asyncio
    async def test_placeholder_fetch_reports_requirement(self):
        client = make_client(lambda r: httpx.Response(200))
        fetcher = WebFetcher(client, crawler=FakeCrawler(_signal_result()))
        ds = DatasetInfo(dataset_id="__auth_required__", name="AUTH", url="",
                         metadata={"auth_notes": "MLS login required"})
        result = await fetcher.fetch(make_source(protocol="web"), ds)
        assert result.raw_bytes is None and result.records is None
        assert any("authentication" in w.lower() for w in result.warnings)


class TestWebFetcherFetch:
    @pytest.mark.asyncio
    async def test_html_table_to_records(self):
        client = make_client(lambda r: html_response(FIXTURES / "table_page.html"))
        fetcher = WebFetcher(client, crawler=FakeCrawler(_signal_result()))
        ds = DatasetInfo(dataset_id="table-0@site.test.gov", name="t", url="https://site.test.gov/data",
                         format_hint="html_table",
                         metadata={"signal": "html_table", "table_index": 0})
        result = await fetcher.fetch(make_source(protocol="web"), ds)
        assert result.format == "html"
        assert result.row_count == 3
        assert result.records[0]["Parcel ID"] == "P-001"
        assert result.records[2]["Tax Amount"] == "1450.75"

    @pytest.mark.asyncio
    async def test_json_blob_to_records(self):
        client = make_client(lambda r: html_response(FIXTURES / "table_page.html"))
        fetcher = WebFetcher(client, crawler=FakeCrawler(_signal_result()))
        ds = DatasetInfo(dataset_id="json-blob@site.test.gov", name="b", url="https://site.test.gov/data",
                         format_hint="json", metadata={"signal": "json_blob"})
        result = await fetcher.fetch(make_source(protocol="web"), ds)
        assert result.format == "json"
        assert result.row_count == 3
        assert result.records[0]["id"] == "P-001"

    @pytest.mark.asyncio
    async def test_download_link_fetched_as_bytes(self):
        body = b"id,name\n1,a\n"
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("parcel.csv"):
                return httpx.Response(200, content=body)
            return html_response(FIXTURES / "table_page.html")

        client = make_client(handler)
        fetcher = WebFetcher(client, crawler=FakeCrawler(_signal_result()))
        ds = DatasetInfo(dataset_id="parcel.csv", name="f", url="https://site.test.gov/downloads/parcel.csv",
                         format_hint="csv", metadata={"signal": "download_link"})
        result = await fetcher.fetch(make_source(protocol="web"), ds)
        assert result.raw_bytes == body
        assert result.format == "csv"

    @pytest.mark.asyncio
    async def test_unfetchable_page_warns(self):
        client = make_client(lambda r: httpx.Response(404))
        fetcher = WebFetcher(client, crawler=FakeCrawler(_signal_result()))
        ds = DatasetInfo(dataset_id="t", name="t", url="https://site.test.gov/gone",
                         metadata={"signal": "html_table"})
        result = await fetcher.fetch(make_source(protocol="web"), ds)
        assert any("could not fetch" in w for w in result.warnings)


class TestWebExtractionHelpers:
    def test_table_index_out_of_range_falls_back_to_largest(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup((FIXTURES / "table_page.html").read_text(encoding="utf-8"), "lxml")
        records = _extract_table_records(soup, table_index=99)
        assert len(records) == 3   # largest table (parcel summary)

    def test_embedded_json_extracts_valid_blobs_only(self):
        from bs4 import BeautifulSoup
        html = """
        <script>var good = [{"id": "item-01", "value": 111, "label": "alpha"}, {"id": "item-02", "value": 222, "label": "bravo"}, {"id": "item-03", "value": 333, "label": "charlie"}, {"id": "item-04", "value": 444, "label": "delta"}, {"id": "item-05", "value": 555, "label": "echo"}];</script>
        <script>var bad = [{not json at all but long enough to trip the length gatexxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx];</script>
        """
        blobs = _extract_embedded_json(BeautifulSoup(html, "lxml"))
        assert len(blobs) == 1 and blobs[0][0]["id"] == "item-01"


# ---------------------------------------------------------------------------
# M4 — Playwright branch (renders a data: URL; skips if browser missing)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_playwright_renders_js_page():
    client = make_client(lambda r: httpx.Response(404))
    fetcher = WebFetcher(client, crawler=FakeCrawler(_signal_result()))
    page_html = (
        "data:text/html,"
        "<html><body><table><tr><th>K</th></tr><tr><td>v1</td></tr><tr><td>v2</td></tr>"
        "</table></body></html>"
    )
    html = await fetcher._playwright_html(page_html)
    if html is None:
        pytest.skip("chromium browser not installed for playwright")
    assert "<table>" in html


# ---------------------------------------------------------------------------
# FetchResult misc
# ---------------------------------------------------------------------------

def test_fetch_result_size_bytes():
    r = FetchResult(source_name="s", dataset_id="d", format="json", records=[{"a": 1}])
    assert r.size_bytes == len(json.dumps([{"a": 1}]))
    r2 = FetchResult(source_name="s", dataset_id="d", format="csv", raw_bytes=b"12345")
    assert r2.size_bytes == 5
