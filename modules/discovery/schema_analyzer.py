"""
Deliverable 1.2 — Schema Analyzer
===================================
Inspects JSON, CSV, and HTML table structures returned by an endpoint
and produces a field mapping proposal against the Master Florida Schema.

Master Florida Schema (from FSD §3)
-------------------------------------
  uuid          UUID       Unique identifier across all sources
  geo_point     Geometry   Lat/Long for mapping
  source_url    String     Original data source for traceability
  standard_score Float     0-100 Livability Score
  last_updated  Timestamp  When the data was last pulled

The analyzer also looks for common domain-specific aliases so they can
be automatically mapped to canonical field names in the transformation
phase.

Usage
-----
  from modules.discovery.schema_analyzer import SchemaAnalyzer

  analyzer = SchemaAnalyzer()
  report = await analyzer.analyze("https://<data-portal>/api/views/<dataset-id>.json")
  print(report)
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Master schema definition
# ---------------------------------------------------------------------------

MASTER_SCHEMA_FIELDS: dict[str, str] = {
    "uuid": "UUID — unique record identifier",
    "geo_point": "Geometry — lat/long point",
    "source_url": "String — traceability back to origin",
    "standard_score": "Float — 0-100 livability score",
    "last_updated": "Timestamp — last data pull",
}

# Candidate alias lists for auto-mapping (canonical_name → [aliases])
FIELD_ALIASES: dict[str, list[str]] = {
    "uuid": ["id", "parcel_id", "folio", "folio_number", "objectid", "globalid", "parcel_no",
             "pin", "ain", "account_number"],
    "geo_point": ["the_geom", "geom", "geometry", "shape", "location", "coordinates",
                  "lat_lon", "longitude_latitude", "point", "geocode"],
    "latitude": ["lat", "y", "y_coord", "latitude_deg"],
    "longitude": ["lon", "lng", "x", "x_coord", "longitude_deg"],
    "address": ["site_addr", "situs_address", "property_address", "addr", "street_address",
                "location_address"],
    "city": ["city_name", "municipality", "muni", "city_nm"],
    "county": ["county_name", "county_nm", "cnty"],
    "zip_code": ["zip", "postal_code", "zipcode", "zip5"],
    "parcel_id": ["folio", "folio_number", "parcel_no", "pin", "ain", "account_number",
                  "objectid"],
    "assessed_value": ["just_value", "just_val", "market_value", "assessed_val",
                       "total_assessment", "total_value"],
    "taxable_value": ["taxable_val", "total_taxable_value", "net_assessed"],
    "tax_amount": ["tax_total", "total_tax", "ad_valorem", "taxes_due"],
    "tax_rate": ["millage", "millage_rate", "effective_tax_rate"],
    "year_built": ["yr_built", "construction_year", "build_year"],
    "sqft": ["total_sqft", "building_sqft", "heated_sqft", "living_area", "sq_ft",
             "square_footage"],
    "bedrooms": ["beds", "bed_rooms", "no_of_bedrooms"],
    "bathrooms": ["baths", "bath_rooms", "no_of_bathrooms"],
    "flood_zone": ["fld_zone", "flood_zone_id", "zone_subtype", "sfha_tf"],
    "school_grade": ["school_rating", "grade", "school_score"],
    "crime_rate": ["crime_index", "crime_score"],
    "last_updated": ["update_dt", "last_edit", "date_modified", "mod_date", "last_change",
                     "updated_at"],
}

# Build reverse lookup: alias → canonical
_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canonical, _aliases in FIELD_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_TO_CANONICAL[_alias.lower()] = _canonical


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class ContentFormat(str, Enum):
    JSON = "json"
    CSV = "csv"
    HTML = "html"
    GEOJSON = "geojson"
    UNKNOWN = "unknown"


@dataclass
class FieldInfo:
    raw_name: str
    canonical_name: str | None        # mapped master-schema name, if found
    sample_values: list[Any]
    inferred_type: str                # "string" | "integer" | "float" | "boolean" | "geometry" | "timestamp" | "unknown"
    null_count: int = 0
    mapping_confidence: float = 0.0   # 0.0–1.0


@dataclass
class SchemaReport:
    endpoint_url: str
    detected_format: ContentFormat
    record_count: int
    fields: list[FieldInfo]
    unmapped_fields: list[str]        # fields with no canonical mapping
    master_schema_coverage: dict[str, str | None]  # master field → raw field name
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "endpoint_url": self.endpoint_url,
            "detected_format": self.detected_format.value,
            "record_count": self.record_count,
            "master_schema_coverage": self.master_schema_coverage,
            "unmapped_master_fields": [k for k, v in self.master_schema_coverage.items() if v is None],
            "unmapped_source_fields": self.unmapped_fields,
            "fields": [
                {
                    "raw_name": f.raw_name,
                    "canonical_name": f.canonical_name,
                    "inferred_type": f.inferred_type,
                    "mapping_confidence": f.mapping_confidence,
                    "sample_values": f.sample_values[:3],
                    "null_count": f.null_count,
                }
                for f in self.fields
            ],
            "notes": self.notes,
        }

    def __str__(self) -> str:
        lines = [
            f"Schema Report: {self.endpoint_url}",
            f"  Format       : {self.detected_format.value}",
            f"  Records      : {self.record_count}",
            "",
            "  Master Schema Coverage:",
        ]
        for master_field, raw in self.master_schema_coverage.items():
            status = f"→ {raw}" if raw else "MISSING"
            lines.append(f"    {master_field:<20} {status}")
        lines += [
            "",
            f"  Unmapped source fields ({len(self.unmapped_fields)}): "
            + (", ".join(self.unmapped_fields[:10]) or "none"),
        ]
        if self.notes:
            lines += ["", "  Notes:"] + [f"    - {n}" for n in self.notes]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class SchemaAnalyzer:
    """
    Fetches an endpoint and produces a SchemaReport describing its fields
    and their mapping to the Master Florida Schema.
    """

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = httpx.Timeout(timeout)

    async def analyze(self, url: str, sample_rows: int = 100) -> SchemaReport:
        """
        Download a sample from `url`, detect format, extract field names,
        infer types, and map to the master schema.
        """
        content, content_type = await self._fetch(url, sample_rows)
        fmt = _detect_format(url, content_type, content)

        if fmt == ContentFormat.JSON or fmt == ContentFormat.GEOJSON:
            records = _parse_json(content)
        elif fmt == ContentFormat.CSV:
            records = _parse_csv(content)
        elif fmt == ContentFormat.HTML:
            records = _parse_html_table(content)
        else:
            return SchemaReport(
                endpoint_url=url,
                detected_format=fmt,
                record_count=0,
                fields=[],
                unmapped_fields=[],
                master_schema_coverage={k: None for k in MASTER_SCHEMA_FIELDS},
                notes=["Could not detect a supported format"],
            )

        return _build_report(url, fmt, records)

    async def analyze_from_catalog(
        self, catalog_path: str, limit: int = 10
    ) -> list[SchemaReport]:
        """
        Runs analysis on the first `limit` entries of a catalog JSON produced
        by SourceDiscoveryEngine, skipping entries without a valid endpoint_url.
        """
        import json
        from pathlib import Path

        data = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
        entries = data.get("entries", [])
        reports: list[SchemaReport] = []

        for entry in entries[:limit]:
            url = entry.get("endpoint_url", "")
            if not url or entry.get("error"):
                continue
            try:
                report = await self.analyze(url)
                reports.append(report)
            except Exception as exc:
                logger.warning("Schema analysis failed for %s: %s", url, exc)

        return reports

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _fetch(self, url: str, sample_rows: int) -> tuple[bytes, str]:
        """Download up to sample_rows records; appends ?$limit= for Socrata."""
        fetch_url = url
        if "/resource/" in url and url.endswith(".json"):
            sep = "&" if "?" in url else "?"
            fetch_url = f"{url}{sep}$limit={sample_rows}"

        async with httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=True
        ) as client:
            response = await client.get(
                fetch_url,
                headers={"User-Agent": "RealEstateMagnet/0.1"},
            )
            response.raise_for_status()
            return response.content, response.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _detect_format(url: str, content_type: str, content: bytes) -> ContentFormat:
    url_lower = url.lower()
    ct_lower = content_type.lower()

    if "geojson" in url_lower or "geojson" in ct_lower:
        return ContentFormat.GEOJSON
    if url_lower.endswith(".json") or "json" in ct_lower:
        try:
            data = json.loads(content)
            if isinstance(data, dict) and data.get("type") in ("FeatureCollection", "Feature"):
                return ContentFormat.GEOJSON
            return ContentFormat.JSON
        except json.JSONDecodeError:
            pass
    if url_lower.endswith(".csv") or "csv" in ct_lower or "text/plain" in ct_lower:
        return ContentFormat.CSV
    if "html" in ct_lower or url_lower.endswith(".html"):
        return ContentFormat.HTML

    # Sniff content
    snippet = content[:512]
    if snippet.strip().startswith(b"<"):
        return ContentFormat.HTML
    if snippet.strip().startswith((b"[", b"{")):
        return ContentFormat.JSON

    return ContentFormat.UNKNOWN


def _parse_json(content: bytes) -> list[dict]:
    data = json.loads(content)
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    # GeoJSON FeatureCollection
    if isinstance(data, dict):
        features = data.get("features", [])
        if features:
            return [f.get("properties", {}) | {"_geometry": f.get("geometry")}
                    for f in features if isinstance(f, dict)]
        # Wrapped result (CKAN style)
        result = data.get("result") or data.get("data") or data.get("records")
        if isinstance(result, list):
            return [r for r in result if isinstance(r, dict)]
    return []


def _parse_csv(content: bytes) -> list[dict]:
    text = content.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return [row for row in reader]


def _parse_html_table(content: bytes) -> list[dict]:
    soup = BeautifulSoup(content, "lxml")
    table = soup.find("table")
    if not table:
        return []
    headers: list[str] = []
    rows: list[dict] = []
    for i, tr in enumerate(table.find_all("tr")):
        cells = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
        if i == 0:
            headers = cells
        elif headers:
            rows.append(dict(zip(headers, cells)))
    return rows


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _build_report(
    url: str, fmt: ContentFormat, records: list[dict]
) -> SchemaReport:
    if not records:
        return SchemaReport(
            endpoint_url=url,
            detected_format=fmt,
            record_count=0,
            fields=[],
            unmapped_fields=[],
            master_schema_coverage={k: None for k in MASTER_SCHEMA_FIELDS},
            notes=["No records returned; cannot infer schema."],
        )

    # Collect all field names across records
    all_keys: list[str] = []
    seen: set[str] = set()
    for record in records:
        for k in record:
            if k not in seen:
                all_keys.append(k)
                seen.add(k)

    fields: list[FieldInfo] = []
    for key in all_keys:
        samples = [r[key] for r in records if key in r and r[key] not in (None, "")]
        null_count = sum(1 for r in records if r.get(key) in (None, ""))
        inferred = _infer_type(samples[:20])
        canonical, confidence = _map_to_canonical(key, samples)
        fields.append(FieldInfo(
            raw_name=key,
            canonical_name=canonical,
            sample_values=samples[:5],
            inferred_type=inferred,
            null_count=null_count,
            mapping_confidence=confidence,
        ))

    # Master schema coverage: find which raw field maps to each master field
    mapped_canonicals: dict[str, str] = {
        f.canonical_name: f.raw_name for f in fields if f.canonical_name
    }
    master_coverage: dict[str, str | None] = {
        mf: mapped_canonicals.get(mf) for mf in MASTER_SCHEMA_FIELDS
    }

    unmapped = [f.raw_name for f in fields if f.canonical_name is None]

    notes: list[str] = []
    if "latitude" in mapped_canonicals and "longitude" in mapped_canonicals and "geo_point" not in mapped_canonicals:
        notes.append("Separate lat/lon fields found — combine into geo_point during transformation.")
    if fmt == ContentFormat.GEOJSON:
        notes.append("GeoJSON geometry detected — geo_point can be extracted directly from 'geometry'.")

    return SchemaReport(
        endpoint_url=url,
        detected_format=fmt,
        record_count=len(records),
        fields=fields,
        unmapped_fields=unmapped,
        master_schema_coverage=master_coverage,
        notes=notes,
    )


def _infer_type(samples: list[Any]) -> str:
    if not samples:
        return "unknown"
    non_null = [s for s in samples if s is not None and s != ""]
    if not non_null:
        return "unknown"
    sample = non_null[0]

    if isinstance(sample, dict):
        keys = set(str(k).lower() for k in sample.keys())
        if keys & {"type", "coordinates"}:
            return "geometry"
        return "object"
    if isinstance(sample, (list,)):
        return "array"
    if isinstance(sample, bool):
        return "boolean"
    if isinstance(sample, int):
        return "integer"
    if isinstance(sample, float):
        return "float"

    # String heuristics
    s = str(sample).strip()
    if _looks_like_timestamp(s):
        return "timestamp"
    if _looks_like_float(s):
        # Check if all non-null samples are numeric
        if all(_looks_like_float(str(v)) for v in non_null[:10]):
            return "float" if "." in s else "integer"
    if s.lower() in ("true", "false", "yes", "no", "y", "n"):
        return "boolean"
    return "string"


def _looks_like_float(s: str) -> bool:
    try:
        float(s.replace(",", ""))
        return True
    except ValueError:
        return False


def _looks_like_timestamp(s: str) -> bool:
    import re
    return bool(re.search(r"\d{4}[-/]\d{2}[-/]\d{2}", s))


def _map_to_canonical(
    raw_name: str, samples: list[Any]
) -> tuple[str | None, float]:
    """
    Returns (canonical_name, confidence) for a raw field name.
    Confidence: 1.0 = exact alias match, 0.8 = fuzzy / substring match.
    """
    key = raw_name.lower().strip()

    # Exact alias match
    if key in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[key], 1.0

    # Exact master schema match
    if key in MASTER_SCHEMA_FIELDS:
        return key, 1.0

    # Substring match against aliases
    for alias, canonical in _ALIAS_TO_CANONICAL.items():
        if alias in key or key in alias:
            return canonical, 0.8

    # Geometry detection via sample value structure
    if samples:
        s = samples[0]
        if isinstance(s, dict) and {"type", "coordinates"} <= set(s.keys()):
            return "geo_point", 0.9

    return None, 0.0
