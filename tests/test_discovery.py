"""
Tests for Phase 1 — Data Discovery module.

Run with:  pytest tests/test_discovery.py -v
"""

from __future__ import annotations

import json
import pytest
import pytest_asyncio
from bs4 import BeautifulSoup

from modules.discovery.florida_sources import (
    ALL_SOURCES, STATEWIDE_SOURCES, COUNTY_SOURCES,
    UNSTRUCTURED_SOURCES, WEB_SOURCES, GATED_SOURCES, JS_REQUIRED_SOURCES,
)
from modules.discovery.web_crawler import (
    WebCrawler, CrawlResult, DataSignal, SignalType,
    _extract_signals_from_html,
    _extract_follow_links,
    _score_download_link,
    _score_html_table,
    _normalise_url,
    _file_extension,
)
from modules.discovery.schema_analyzer import (
    SchemaAnalyzer,
    SchemaReport,
    ContentFormat,
    _detect_format,
    _parse_json,
    _parse_csv,
    _map_to_canonical,
    _build_report,
)


# ---------------------------------------------------------------------------
# florida_sources.py — unit tests (no I/O)
# ---------------------------------------------------------------------------

class TestFloridaSources:
    def test_all_sources_non_empty(self):
        assert len(ALL_SOURCES) > 0

    def test_statewide_and_county_combine(self):
        assert len(ALL_SOURCES) == len(STATEWIDE_SOURCES) + len(COUNTY_SOURCES) + len(UNSTRUCTURED_SOURCES)

    def test_each_source_has_required_fields(self):
        valid_protocols = ("socrata", "arcgis", "ckan", "direct", "web")
        for source in ALL_SOURCES:
            assert source.name, f"Source missing name: {source}"
            assert source.base_url.startswith("http"), f"Bad URL for {source.name}"
            assert source.protocol in valid_protocols, f"Unknown protocol for {source.name}"
            assert len(source.categories) > 0, f"No categories for {source.name}"

    def test_socrata_sources_have_domain(self):
        socrata = [s for s in ALL_SOURCES if s.protocol == "socrata"]
        assert len(socrata) >= 2
        for s in socrata:
            assert s.socrata_domain, f"Socrata source missing domain: {s.name}"

    def test_categories_are_valid(self):
        valid = {
            "property", "tax", "flood", "schools", "crime", "transit",
            "geospatial", "hoa", "community", "legal", "misc"
        }
        for source in ALL_SOURCES:
            for cat in source.categories:
                assert cat in valid, f"Unknown category '{cat}' in {source.name}"

    def test_web_sources_non_empty(self):
        assert len(WEB_SOURCES) >= 5, "Expected several unstructured web sources"

    def test_web_sources_have_crawl_depth(self):
        for s in WEB_SOURCES:
            assert s.crawl_depth >= 1, f"Web source missing crawl_depth: {s.name}"

    def test_gated_sources_have_auth_notes(self):
        for s in GATED_SOURCES:
            assert s.auth_notes, f"Gated source missing auth_notes: {s.name}"

    def test_unstructured_sources_protocol(self):
        for s in UNSTRUCTURED_SOURCES:
            assert s.protocol == "web", f"Unstructured source has wrong protocol: {s.name}"

    def test_convenience_lists_are_subsets(self):
        all_set = set(id(s) for s in ALL_SOURCES)
        assert all(id(s) in all_set for s in WEB_SOURCES)
        assert all(id(s) in all_set for s in GATED_SOURCES)
        assert all(id(s) in all_set for s in JS_REQUIRED_SOURCES)


# ---------------------------------------------------------------------------
# Schema Analyzer — unit tests (no network)
# ---------------------------------------------------------------------------

class TestDetectFormat:
    def test_json_url(self):
        assert _detect_format("https://example.com/data.json", "application/json", b"[]") == ContentFormat.JSON

    def test_csv_url(self):
        assert _detect_format("https://example.com/data.csv", "text/csv", b"a,b\n1,2") == ContentFormat.CSV

    def test_html_content_type(self):
        assert _detect_format("https://example.com/", "text/html", b"<html>") == ContentFormat.HTML

    def test_geojson_content_type(self):
        result = _detect_format(
            "https://example.com/data.geojson",
            "application/geo+json",
            json.dumps({"type": "FeatureCollection", "features": []}).encode(),
        )
        assert result == ContentFormat.GEOJSON

    def test_json_feature_collection_sniffed(self):
        payload = json.dumps({"type": "FeatureCollection", "features": []}).encode()
        result = _detect_format("https://example.com/data.json", "application/json", payload)
        assert result == ContentFormat.GEOJSON


class TestParseJson:
    def test_flat_list(self):
        records = _parse_json(json.dumps([{"a": 1}, {"a": 2}]).encode())
        assert len(records) == 2
        assert records[0]["a"] == 1

    def test_geojson_feature_collection(self):
        fc = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-80, 25]},
                 "properties": {"name": "Miami"}}
            ],
        }
        records = _parse_json(json.dumps(fc).encode())
        assert len(records) == 1
        assert records[0]["name"] == "Miami"
        assert "_geometry" in records[0]

    def test_wrapped_result(self):
        records = _parse_json(json.dumps({"result": [{"x": 1}]}).encode())
        assert records[0]["x"] == 1


class TestParseCsv:
    def test_basic_csv(self):
        csv_bytes = b"parcel_id,just_value,address\nABC123,250000,123 Main St\n"
        records = _parse_csv(csv_bytes)
        assert len(records) == 1
        assert records[0]["parcel_id"] == "ABC123"
        assert records[0]["just_value"] == "250000"


class TestMapToCanonical:
    @pytest.mark.parametrize("raw,expected_canonical", [
        ("folio", "parcel_id"),
        ("just_value", "assessed_value"),
        ("millage_rate", "tax_rate"),
        ("the_geom", "geo_point"),
        ("lat", "latitude"),
        ("updated_at", "last_updated"),
        ("uuid", "uuid"),
    ])
    def test_known_aliases(self, raw, expected_canonical):
        canonical, confidence = _map_to_canonical(raw, [])
        assert canonical == expected_canonical
        assert confidence >= 0.8

    def test_unknown_field_returns_none(self):
        # Use a field name that has no single-char overlap with any alias
        canonical, confidence = _map_to_canonical("zzzfoo_qqqbar_nnn", [])
        assert canonical is None
        assert confidence == 0.0

    def test_geometry_sample_detection(self):
        # "shape_obj" matches "shape" alias (substring) → geo_point at 0.8
        samples = [{"type": "Point", "coordinates": [-80.19, 25.77]}]
        canonical, confidence = _map_to_canonical("shape_obj", samples)
        assert canonical == "geo_point"
        assert confidence >= 0.8


class TestBuildReport:
    def _make_records(self) -> list[dict]:
        return [
            {
                "folio": "01-4138-000-0010",
                "just_value": "450000",
                "site_addr": "100 NW 1st Ave",
                "city_name": "Miami",
                "zip": "33128",
                "millage_rate": "18.5",
                "yr_built": "1995",
                "the_geom": {"type": "Point", "coordinates": [-80.19, 25.77]},
                "updated_at": "2024-01-15T00:00:00Z",
            }
            for _ in range(5)
        ]

    def test_report_fields_present(self):
        records = self._make_records()
        report = _build_report("https://example.com/test.json", ContentFormat.JSON, records)
        assert report.record_count == 5
        raw_names = {f.raw_name for f in report.fields}
        assert "folio" in raw_names
        assert "just_value" in raw_names

    def test_canonical_mapping(self):
        records = self._make_records()
        report = _build_report("https://example.com/test.json", ContentFormat.JSON, records)
        canonicals = {f.canonical_name for f in report.fields}
        assert "parcel_id" in canonicals      # folio
        assert "assessed_value" in canonicals  # just_value
        assert "geo_point" in canonicals       # the_geom

    def test_master_coverage_geo_point(self):
        records = self._make_records()
        report = _build_report("https://example.com/test.json", ContentFormat.JSON, records)
        assert report.master_schema_coverage.get("geo_point") is not None

    def test_empty_records(self):
        report = _build_report("https://example.com/empty.json", ContentFormat.JSON, [])
        assert report.record_count == 0
        assert all(v is None for v in report.master_schema_coverage.values())

    def test_report_to_dict(self):
        records = self._make_records()
        report = _build_report("https://example.com/test.json", ContentFormat.JSON, records)
        d = report.to_dict()
        assert "fields" in d
        assert "master_schema_coverage" in d
        assert isinstance(d["fields"], list)


# ---------------------------------------------------------------------------
# SourceDiscoveryEngine — lightweight integration smoke test
# (uses pytest-httpx to mock HTTP; skipped if not installed)
# ---------------------------------------------------------------------------

try:
    import pytest_httpx  # noqa: F401
    HAS_HTTPX_MOCK = True
except ImportError:
    HAS_HTTPX_MOCK = False


@pytest.mark.skipif(not HAS_HTTPX_MOCK, reason="pytest-httpx not installed")
@pytest.mark.asyncio
async def test_discovery_engine_direct_source():
    """Direct-protocol sources should return one entry without any HTTP call."""
    from modules.discovery.source_discovery import SourceDiscoveryEngine
    from modules.discovery.florida_sources import FloridaSource

    direct_source = FloridaSource(
        name="Test Direct",
        base_url="https://example.com/data",
        protocol="direct",
        categories=["property"],
        notes="Test only",
    )
    engine = SourceDiscoveryEngine(sources=[direct_source])
    catalog = await engine.run()
    assert len(catalog) == 1
    assert catalog[0]["protocol"] == "direct"
    assert catalog[0]["endpoint_url"] == "https://example.com/data"


# ---------------------------------------------------------------------------
# WebCrawler — unit tests (no network)
# ---------------------------------------------------------------------------

class TestUrlHelpers:
    @pytest.mark.parametrize("url,expected", [
        ("https://example.com/data.csv", ".csv"),
        ("https://example.com/report.xlsx", ".xlsx"),
        ("https://example.com/map.geojson?v=1", ".geojson"),
        ("https://example.com/page", ""),
        ("https://example.com/path/file.ZIP", ".zip"),
    ])
    def test_file_extension(self, url, expected):
        assert _file_extension(url) == expected

    def test_normalise_url_strips_fragment(self):
        url = "https://example.com/page#section"
        assert "#" not in _normalise_url(url)

    def test_normalise_url_strips_trailing_slash(self):
        assert _normalise_url("https://example.com/path/") == "https://example.com/path"


class TestScoring:
    def test_geojson_link_scores_highest(self):
        score = _score_download_link("https://example.com/parcels.geojson", "Download GeoJSON")
        assert score >= 0.90

    def test_pdf_link_scores_low(self):
        score = _score_download_link("https://example.com/report.pdf", "Annual Report")
        assert score < 0.50

    def test_fl_keyword_boosts_score(self):
        score_fl = _score_download_link("https://example.com/parcel-data.csv", "Parcel Data")
        score_generic = _score_download_link("https://example.com/data.csv", "Download")
        assert score_fl >= score_generic

    def test_html_table_score_grows_with_rows(self):
        score_small = _score_html_table("Name Address", rows=5, cols=3)
        score_large = _score_html_table("Name Address", rows=200, cols=8)
        assert score_large > score_small

    def test_fl_keyword_boosts_table_score(self):
        s_fl = _score_html_table("Parcel ID Assessed Value Tax", rows=50, cols=5)
        s_generic = _score_html_table("Col1 Col2 Col3", rows=50, cols=5)
        assert s_fl > s_generic


class TestExtractSignals:
    def _make_soup(self, html: str) -> "BeautifulSoup":
        return BeautifulSoup(html, "lxml")

    def test_detects_csv_download_link(self):
        html = '<html><body><a href="/data/parcels.csv">Download CSV</a></body></html>'
        soup = self._make_soup(html)
        signals = _extract_signals_from_html("https://example.com/", soup, "https://example.com/")
        dl = [s for s in signals if s.signal_type == SignalType.DOWNLOAD_LINK]
        assert len(dl) == 1
        assert dl[0].format_hint == "csv"
        assert dl[0].url.endswith("parcels.csv")

    def test_detects_xlsx_download_link(self):
        html = '<html><body><a href="/reports/taxes.xlsx">Tax Report</a></body></html>'
        soup = self._make_soup(html)
        signals = _extract_signals_from_html("https://example.com/", soup, "https://example.com/")
        dl = [s for s in signals if s.signal_type == SignalType.DOWNLOAD_LINK]
        assert any(s.format_hint == "xlsx" for s in dl)

    def test_detects_html_table_with_enough_rows(self):
        rows = "".join(f"<tr><td>Parcel {i}</td><td>${i*1000}</td></tr>" for i in range(10))
        html = f"<html><body><table><tr><th>Parcel ID</th><th>Value</th></tr>{rows}</table></body></html>"
        soup = self._make_soup(html)
        signals = _extract_signals_from_html("https://example.com/results", soup, "https://example.com/")
        tables = [s for s in signals if s.signal_type == SignalType.HTML_TABLE]
        assert len(tables) == 1
        assert tables[0].table_row_count >= 10

    def test_skips_small_tables(self):
        html = "<html><body><table><tr><td>a</td><td>b</td></tr></table></body></html>"
        soup = self._make_soup(html)
        signals = _extract_signals_from_html("https://example.com/", soup, "https://example.com/")
        tables = [s for s in signals if s.signal_type == SignalType.HTML_TABLE]
        assert len(tables) == 0

    def test_detects_search_form(self):
        html = (
            '<html><body>'
            '<form action="/search/property" method="GET">'
            '<input name="parcel_id"><button>Search</button>'
            '</form></body></html>'
        )
        soup = self._make_soup(html)
        signals = _extract_signals_from_html("https://example.com/", soup, "https://example.com/")
        forms = [s for s in signals if s.signal_type == SignalType.FORM]
        assert len(forms) == 1
        assert "GET" in forms[0].extra.get("method", "")

    def test_detects_embedded_json_blob(self):
        json_data = json.dumps([{"parcel": "ABC", "value": 100000}] * 10)
        html = f"<html><body><script>var propertyData = {json_data};</script></body></html>"
        soup = self._make_soup(html)
        signals = _extract_signals_from_html("https://example.com/", soup, "https://example.com/")
        blobs = [s for s in signals if s.signal_type == SignalType.JSON_BLOB]
        assert len(blobs) == 1
        assert blobs[0].extra.get("item_count", 0) >= 10

    def test_detects_fetch_api_call(self):
        html = (
            '<html><body>'
            '<script>fetch("/api/v1/properties?county=miami")</script>'
            '</body></html>'
        )
        soup = self._make_soup(html)
        signals = _extract_signals_from_html("https://example.com/", soup, "https://example.com/")
        apis = [s for s in signals if s.signal_type == SignalType.API_PATTERN]
        assert len(apis) >= 1
        assert "properties" in apis[0].url


class TestExtractFollowLinks:
    def _make_soup(self, html: str) -> "BeautifulSoup":
        return BeautifulSoup(html, "lxml")

    def test_follows_data_relevant_links(self):
        html = (
            '<html><body>'
            '<a href="/search/parcel">Parcel Search</a>'
            '<a href="/download/data">Data Download</a>'
            '<a href="/about">About Us</a>'
            '<a href="https://external.com/data">External</a>'
            '</body></html>'
        )
        soup = self._make_soup(html)
        links = _extract_follow_links("https://example.com/", soup, "https://example.com/")
        assert any("parcel" in l for l in links)
        assert any("download" in l for l in links)
        # about and external should be excluded
        assert not any("about" in l for l in links)
        assert not any("external.com" in l for l in links)

    def test_skips_css_and_js_files(self):
        html = (
            '<html><body>'
            '<a href="/styles/main.css">CSS</a>'
            '<a href="/js/app.js">JS</a>'
            '<a href="/data/properties.csv">Properties</a>'
            '</body></html>'
        )
        soup = self._make_soup(html)
        links = _extract_follow_links("https://example.com/", soup, "https://example.com/")
        assert not any(".css" in l for l in links)
        assert not any(".js" in l for l in links)


class TestCrawlResult:
    def test_top_signals_sorted_by_relevance(self):
        signals = [
            DataSignal(url="a", signal_type=SignalType.DOWNLOAD_LINK, format_hint="csv", relevance_score=0.5),
            DataSignal(url="b", signal_type=SignalType.DOWNLOAD_LINK, format_hint="geojson", relevance_score=0.95),
            DataSignal(url="c", signal_type=SignalType.HTML_TABLE, format_hint="html_table", relevance_score=0.70),
        ]
        result = CrawlResult(
            seed_url="https://example.com",
            source_name="Test",
            pages_visited=3,
            signals=signals,
            gated_urls=[],
            robots_blocked=[],
            errors=[],
        )
        top = result.top_signals
        assert top[0].relevance_score == 0.95
        assert top[-1].relevance_score == 0.5

    def test_to_dict_structure(self):
        result = CrawlResult(
            seed_url="https://example.com",
            source_name="Test",
            pages_visited=1,
            signals=[],
            gated_urls=["https://example.com/login"],
            robots_blocked=[],
            errors=[],
        )
        d = result.to_dict()
        assert d["pages_visited"] == 1
        assert "signals" in d
        assert d["gated_urls"] == ["https://example.com/login"]


@pytest.mark.asyncio
async def test_crawler_gated_source_without_cookies():
    """A gated source crawled without auth cookies should note the requirement."""
    from modules.discovery.florida_sources import FloridaSource

    gated = FloridaSource(
        name="Test Gated",
        base_url="https://example.com/mls",
        protocol="web",
        categories=["property"],
        requires_auth=True,
        auth_notes="Requires agent login.",
        crawl_depth=1,
    )
    crawler = WebCrawler(max_pages=1)
    # Patch the HTTP call to avoid real network access
    import unittest.mock as mock
    with mock.patch.object(crawler, "_crawl_with_httpx", new_callable=mock.AsyncMock) as m:
        m.return_value = None
        result = await crawler.crawl(gated, auth_cookies=None)
        assert any("auth" in n.lower() or "requires" in n.lower() for n in result.notes)


@pytest.mark.asyncio
async def test_discovery_engine_excludes_web_by_default():
    """SourceDiscoveryEngine with default settings excludes web-protocol sources."""
    from modules.discovery.source_discovery import SourceDiscoveryEngine
    from modules.discovery.florida_sources import FloridaSource

    web_source = FloridaSource(
        name="Test Web",
        base_url="https://example.com/",
        protocol="web",
        categories=["hoa"],
        crawl_depth=1,
    )
    engine = SourceDiscoveryEngine(sources=[web_source], include_unstructured=False)
    assert len(engine.sources) == 0  # filtered out


@pytest.mark.asyncio
async def test_discovery_engine_includes_web_when_flag_set():
    """SourceDiscoveryEngine with include_unstructured=True keeps web sources."""
    from modules.discovery.source_discovery import SourceDiscoveryEngine
    from modules.discovery.florida_sources import FloridaSource

    web_source = FloridaSource(
        name="Test Web",
        base_url="https://example.com/",
        protocol="web",
        categories=["hoa"],
        crawl_depth=1,
    )
    engine = SourceDiscoveryEngine(sources=[web_source], include_unstructured=True)
    assert len(engine.sources) == 1
