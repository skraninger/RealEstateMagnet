"""SQLite database layer for condensed community data (FSD §3b).

Provides a relational database for storing and querying community information
without requiring the full PostgreSQL/PostGIS stack. Uses SQLAlchemy 2.0+
declarative models with SQLite as the backend.

Data flows in from:
- The file-based CommunityStore (import)
- The AI condenser (direct upsert)
- The research agent (direct upsert)

Usage::

    db = CommunityDatabase("data/communities.db")
    db.create_tables()
    db.upsert_record(record)
    results = db.query_communities(county="miami-dade", has_golf=True)
"""

from __future__ import annotations

import csv
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Session,
    relationship,
    sessionmaker,
)

from .models import (
    AmenityFact,
    CommunityFacts,
    CommunityIdentity,
    CommunityRecord,
    Demographics,
    Discrepancy,
    FeeFact,
    ProximityMetric,
    ResearchLogEntry,
    utcnow,
)
from .store import slugify

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data") / "communities.db"


class Base(DeclarativeBase):
    pass


class CommunityRow(Base):
    __tablename__ = "communities"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False, index=True)
    slug = Column(String(255), unique=True, nullable=False, index=True)
    county_fips = Column(String(10), index=True)
    city = Column(String(100), index=True)
    hoa_name = Column(String(255))
    cdd_name = Column(String(255))
    is_gated = Column(Boolean, default=True, index=True)
    latitude = Column(Float)
    longitude = Column(Float)
    overview = Column(Text)
    notes = Column(Text)
    data_source = Column(String(50), default="file_store")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    fees = relationship("FeeRow", back_populates="community", cascade="all, delete-orphan")
    amenities = relationship("AmenityRow", back_populates="community", cascade="all, delete-orphan")
    demographics_entries = relationship("DemographicsRow", back_populates="community", cascade="all, delete-orphan")
    proximity_entries = relationship("ProximityRow", back_populates="community", cascade="all, delete-orphan")

    def to_record(self) -> CommunityRecord:
        geo_point = None
        if self.latitude is not None and self.longitude is not None:
            geo_point = {"lat": self.latitude, "lng": self.longitude}

        identity = CommunityIdentity(
            name=self.name,
            slug=self.slug,
            county_fips=self.county_fips,
            city=self.city,
            hoa_name=self.hoa_name,
            cdd_name=self.cdd_name,
            is_gated=self.is_gated,
            geo_point=geo_point,
            notes=self.notes,
        )

        fees = [
            FeeFact(
                fee_type=f.fee_type,
                amount=f.amount,
                period=f.period,
                currency=f.currency or "USD",
                source_url=f.source_url or "https://placeholder",
                retrieved_at=f.retrieved_at or datetime.now(timezone.utc),
                confidence=f.confidence or 0.5,
                note=f.note,
            )
            for f in self.fees
        ]

        amenities = [
            AmenityFact(
                amenity=a.amenity,
                detail=a.detail,
                source_url=a.source_url or "https://placeholder",
                retrieved_at=a.retrieved_at or datetime.now(timezone.utc),
                confidence=a.confidence or 0.5,
            )
            for a in self.amenities
        ]

        demographics = None
        if self.demographics_entries:
            latest = max(self.demographics_entries, key=lambda d: d.retrieved_at or datetime.min.replace(tzinfo=timezone.utc))
            demographics = Demographics(
                median_age=latest.median_age,
                median_household_income=latest.median_household_income,
                owner_occupancy_pct=latest.owner_occupancy_pct,
                population=latest.population,
                data_year=latest.data_year,
                geography_level=latest.geography_level,
                source_url=latest.source_url or "https://placeholder",
                retrieved_at=latest.retrieved_at or datetime.now(timezone.utc),
                confidence=latest.confidence or 0.5,
            )

        proximity = [
            ProximityMetric(
                category=p.category,
                nearest_name=p.nearest_name,
                distance_miles=p.distance_miles,
                source_url=p.source_url or "https://placeholder",
                retrieved_at=p.retrieved_at or datetime.now(timezone.utc),
                confidence=p.confidence or 0.5,
            )
            for p in self.proximity_entries
        ]

        return CommunityRecord(
            id=uuid.UUID(self.id),
            identity=identity,
            fees=fees,
            amenities=amenities,
            demographics=demographics,
            proximity=proximity,
            created_at=self.created_at or datetime.now(timezone.utc),
            updated_at=self.updated_at or datetime.now(timezone.utc),
        )


class FeeRow(Base):
    __tablename__ = "community_fees"

    id = Column(Integer, primary_key=True, autoincrement=True)
    community_slug = Column(String(255), ForeignKey("communities.slug"), nullable=False, index=True)
    fee_type = Column(String(50), nullable=False, index=True)
    amount = Column(Float)
    period = Column(String(20))
    currency = Column(String(10), default="USD")
    source_url = Column(Text)
    retrieved_at = Column(DateTime)
    confidence = Column(Float, default=0.5)
    note = Column(Text)

    community = relationship("CommunityRow", back_populates="fees")


class AmenityRow(Base):
    __tablename__ = "community_amenities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    community_slug = Column(String(255), ForeignKey("communities.slug"), nullable=False, index=True)
    amenity = Column(String(100), nullable=False, index=True)
    detail = Column(Text)
    source_url = Column(Text)
    retrieved_at = Column(DateTime)
    confidence = Column(Float, default=0.5)

    community = relationship("CommunityRow", back_populates="amenities")


class DemographicsRow(Base):
    __tablename__ = "community_demographics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    community_slug = Column(String(255), ForeignKey("communities.slug"), nullable=False, index=True)
    median_age = Column(Float)
    median_household_income = Column(Float)
    owner_occupancy_pct = Column(Float)
    population = Column(Integer)
    data_year = Column(Integer)
    geography_level = Column(String(50))
    source_url = Column(Text)
    retrieved_at = Column(DateTime)
    confidence = Column(Float, default=0.5)

    community = relationship("CommunityRow", back_populates="demographics_entries")


class ProximityRow(Base):
    __tablename__ = "proximity_metrics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    community_slug = Column(String(255), ForeignKey("communities.slug"), nullable=False, index=True)
    category = Column(String(50), nullable=False, index=True)
    nearest_name = Column(String(255))
    distance_miles = Column(Float)
    source_url = Column(Text)
    retrieved_at = Column(DateTime)
    confidence = Column(Float, default=0.5)

    community = relationship("CommunityRow", back_populates="proximity_entries")


class SourceURLRow(Base):
    """Tracks every URL discovered during research with browser access status.
    
    Each URL is accessed via a visible (non-headless) browser controlled by the
    vision-driven LLM agent. The page is printed to PDF and cross-referenced here.
    4xx failures are flagged for manual review.
    """
    __tablename__ = "source_urls"

    id = Column(Integer, primary_key=True, autoincrement=True)
    url = Column(Text, unique=True, nullable=False, index=True)
    domain = Column(String(255), index=True)
    title = Column(String(500))
    
    # Access status
    status = Column(String(30), nullable=False, index=True, default="pending")
    # pending | accessed | captcha_solving | printed | failed_4xx | failed_5xx | failed_timeout | failed_network | blocked
    
    # Failure tracking (for 4xx review queue)
    failure_category = Column(String(30), index=True)
    # cloudflare_block | bot_detection | auth_required | not_found | rate_limited | forbidden | bad_request | geo_blocked | timeout | network_error | unknown
    failure_detail = Column(Text)
    is_retryable = Column(Boolean, default=False)
    reviewed = Column(Boolean, default=False, index=True)
    review_note = Column(Text)
    
    # Artifacts
    pdf_path = Column(Text)
    screenshot_path = Column(Text)
    
    # Context
    community_slug = Column(String(255), index=True)
    discovered_by = Column(String(50), index=True)
    # web_search | google_search | web_condenser | browser_condenser | research
    
    # Timestamps
    discovered_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    accessed_at = Column(DateTime)
    printed_at = Column(DateTime)
    
    # HTTP/metadata
    http_status = Column(Integer)
    error_message = Column(Text)
    content_length = Column(Integer)
    
    # CAPTCHA tracking
    captcha_detected = Column(Boolean, default=False)
    captcha_solved = Column(Boolean, default=False)
    
    # Robot-friendly tracking
    robot_friendly = Column(Boolean, default=False, index=True)
    robots_txt_checked = Column(Boolean, default=False)
    
    # Execution metadata
    retry_count = Column(Integer, default=0)
    steps_taken = Column(Integer, default=0)


class CommunityDatabase:
    """SQLite database for storing and querying condensed community data."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )
        self._Session = sessionmaker(bind=self.engine)

    def create_tables(self) -> None:
        Base.metadata.create_all(self.engine)

    def drop_tables(self) -> None:
        Base.metadata.drop_all(self.engine)

    def session(self) -> Session:
        return self._Session()

    def upsert_record(self, record: CommunityRecord, data_source: str = "file_store") -> str:
        with self.session() as session:
            existing = session.get(CommunityRow, str(record.id))
            if existing is not None:
                self._update_row(existing, record, session)
                session.commit()
                return existing.slug

            row = CommunityRow(
                id=str(record.id),
                name=record.identity.name,
                slug=record.identity.slug,
                county_fips=record.identity.county_fips,
                city=record.identity.city,
                hoa_name=record.identity.hoa_name,
                cdd_name=record.identity.cdd_name,
                is_gated=record.identity.is_gated,
                notes=record.identity.notes,
                data_source=data_source,
            )
            if record.identity.geo_point:
                row.latitude = record.identity.geo_point.get("lat")
                row.longitude = record.identity.geo_point.get("lng")

            for fee in record.fees:
                row.fees.append(FeeRow(
                    fee_type=fee.fee_type,
                    amount=fee.amount,
                    period=fee.period,
                    currency=fee.currency,
                    source_url=fee.source_url,
                    retrieved_at=fee.retrieved_at,
                    confidence=fee.confidence,
                    note=fee.note,
                ))

            for amenity in record.amenities:
                row.amenities.append(AmenityRow(
                    amenity=amenity.amenity,
                    detail=amenity.detail,
                    source_url=amenity.source_url,
                    retrieved_at=amenity.retrieved_at,
                    confidence=amenity.confidence,
                ))

            if record.demographics is not None:
                d = record.demographics
                row.demographics_entries.append(DemographicsRow(
                    median_age=d.median_age,
                    median_household_income=d.median_household_income,
                    owner_occupancy_pct=d.owner_occupancy_pct,
                    population=d.population,
                    data_year=d.data_year,
                    geography_level=d.geography_level,
                    source_url=d.source_url,
                    retrieved_at=d.retrieved_at,
                    confidence=d.confidence,
                ))

            for prox in record.proximity:
                row.proximity_entries.append(ProximityRow(
                    category=prox.category,
                    nearest_name=prox.nearest_name,
                    distance_miles=prox.distance_miles,
                    source_url=prox.source_url,
                    retrieved_at=prox.retrieved_at,
                    confidence=prox.confidence,
                ))

            session.add(row)
            session.commit()
            return row.slug

    def _update_row(self, row: CommunityRow, record: CommunityRecord, session: Session) -> None:
        row.name = record.identity.name
        row.county_fips = record.identity.county_fips
        row.city = record.identity.city
        row.hoa_name = record.identity.hoa_name
        row.cdd_name = record.identity.cdd_name
        row.is_gated = record.identity.is_gated
        row.notes = record.identity.notes
        if record.identity.geo_point:
            row.latitude = record.identity.geo_point.get("lat")
            row.longitude = record.identity.geo_point.get("lng")
        row.updated_at = datetime.now(timezone.utc)

        existing_fee_keys = {
            (f.fee_type, f.amount, f.period, f.source_url)
            for f in row.fees
        }
        for fee in record.fees:
            key = (fee.fee_type, fee.amount, fee.period, fee.source_url)
            if key not in existing_fee_keys:
                row.fees.append(FeeRow(
                    fee_type=fee.fee_type,
                    amount=fee.amount,
                    period=fee.period,
                    currency=fee.currency,
                    source_url=fee.source_url,
                    retrieved_at=fee.retrieved_at,
                    confidence=fee.confidence,
                    note=fee.note,
                ))
                existing_fee_keys.add(key)

        existing_amenity_keys = {
            (a.amenity, a.detail, a.source_url)
            for a in row.amenities
        }
        for amenity in record.amenities:
            key = (amenity.amenity, amenity.detail, amenity.source_url)
            if key not in existing_amenity_keys:
                row.amenities.append(AmenityRow(
                    amenity=amenity.amenity,
                    detail=amenity.detail,
                    source_url=amenity.source_url,
                    retrieved_at=amenity.retrieved_at,
                    confidence=amenity.confidence,
                ))
                existing_amenity_keys.add(key)

        if record.demographics is not None:
            d = record.demographics
            row.demographics_entries.append(DemographicsRow(
                median_age=d.median_age,
                median_household_income=d.median_household_income,
                owner_occupancy_pct=d.owner_occupancy_pct,
                population=d.population,
                data_year=d.data_year,
                geography_level=d.geography_level,
                source_url=d.source_url,
                retrieved_at=d.retrieved_at,
                confidence=d.confidence,
            ))

        existing_prox_keys = {
            (p.category, p.nearest_name, round(p.distance_miles or 0, 2), p.source_url)
            for p in row.proximity_entries
        }
        for prox in record.proximity:
            key = (prox.category, prox.nearest_name, round(prox.distance_miles or 0, 2), prox.source_url)
            if key not in existing_prox_keys:
                row.proximity_entries.append(ProximityRow(
                    category=prox.category,
                    nearest_name=prox.nearest_name,
                    distance_miles=prox.distance_miles,
                    source_url=prox.source_url,
                    retrieved_at=prox.retrieved_at,
                    confidence=prox.confidence,
                ))
                existing_prox_keys.add(key)

    def load_record(self, slug: str) -> Optional[CommunityRecord]:
        with self.session() as session:
            row = session.query(CommunityRow).filter_by(slug=slug).first()
            if row is None:
                return None
            return row.to_record()

    def count(self) -> int:
        with self.session() as session:
            return session.query(func.count(CommunityRow.id)).scalar()

    def list_slugs(self) -> list[str]:
        with self.session() as session:
            rows = session.query(CommunityRow.slug).all()
            return [r[0] for r in rows]

    def list_records(self, limit: int = 1000) -> list[CommunityRecord]:
        """List all community records from the database."""
        with self.session() as session:
            rows = session.query(CommunityRow).order_by(CommunityRow.name).limit(limit).all()
            return [row.to_record() for row in rows]

    def query_communities(
        self,
        county_fips: Optional[str] = None,
        city: Optional[str] = None,
        is_gated: Optional[bool] = None,
        has_amenity: Optional[str] = None,
        min_hoa_monthly: Optional[float] = None,
        max_hoa_monthly: Optional[float] = None,
        data_source: Optional[str] = None,
        limit: int = 100,
    ) -> list[CommunityRecord]:
        with self.session() as session:
            q = session.query(CommunityRow)
            if county_fips is not None:
                q = q.filter(CommunityRow.county_fips == county_fips)
            if city is not None:
                q = q.filter(CommunityRow.city.ilike(f"%{city}%"))
            if is_gated is not None:
                q = q.filter(CommunityRow.is_gated == is_gated)
            if data_source is not None:
                q = q.filter(CommunityRow.data_source == data_source)
            if has_amenity is not None:
                amenity_slugs = {
                    r[0]
                    for r in session.query(AmenityRow.community_slug)
                    .filter(AmenityRow.amenity == has_amenity)
                    .all()
                }
                q = q.filter(CommunityRow.slug.in_(amenity_slugs))
            if min_hoa_monthly is not None or max_hoa_monthly is not None:
                hoa_slugs = set()
                fee_q = session.query(FeeRow.community_slug, FeeRow.amount).filter(
                    FeeRow.fee_type == "hoa_monthly"
                )
                for slug_val, amount in fee_q.all():
                    if amount is None:
                        continue
                    if min_hoa_monthly is not None and amount < min_hoa_monthly:
                        continue
                    if max_hoa_monthly is not None and amount > max_hoa_monthly:
                        continue
                    hoa_slugs.add(slug_val)
                q = q.filter(CommunityRow.slug.in_(hoa_slugs))

            q = q.order_by(CommunityRow.name).limit(limit)
            return [row.to_record() for row in q.all()]

    def import_from_store(self, store_root: Path | str = Path("data") / "communities") -> int:
        from .store import CommunityStore

        store = CommunityStore(store_root)
        count = 0
        for slug in self._list_store_slugs(store):
            record = store.load_community(slug)
            if record is not None:
                self.upsert_record(record, data_source="file_store")
                count += 1
        logger.info("Imported %d communities from file store", count)
        return count

    def _list_store_slugs(self, store) -> list[str]:
        if not store.communities_dir.exists():
            return []
        return [
            p.stem
            for p in store.communities_dir.glob("*.json")
            if p.stem != "target_list"
        ]

    def export_csv(self, output_path: Path | str, records: Optional[list[CommunityRecord]] = None) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if records is None:
            with self.session() as session:
                rows = session.query(CommunityRow).order_by(CommunityRow.name).all()
                records = [r.to_record() for r in rows]

        fieldnames = [
            "name", "slug", "city", "county_fips", "is_gated", "latitude", "longitude",
            "hoa_monthly", "hoa_annual", "cdd_assessment",
            "amenities", "median_age", "median_income", "owner_occupancy_pct", "population",
            "data_source",
        ]
        with output_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for rec in records:
                row = {
                    "name": rec.identity.name,
                    "slug": rec.identity.slug,
                    "city": rec.identity.city or "",
                    "county_fips": rec.identity.county_fips or "",
                    "is_gated": rec.identity.is_gated,
                    "latitude": "",
                    "longitude": "",
                    "hoa_monthly": "",
                    "hoa_annual": "",
                    "cdd_assessment": "",
                    "amenities": "",
                    "median_age": "",
                    "median_income": "",
                    "owner_occupancy_pct": "",
                    "population": "",
                    "data_source": "",
                }
                if rec.identity.geo_point:
                    row["latitude"] = rec.identity.geo_point.get("lat", "")
                    row["longitude"] = rec.identity.geo_point.get("lng", "")

                for fee in rec.fees:
                    if fee.fee_type in ("hoa_monthly", "hoa_annual", "cdd_assessment"):
                        row[fee.fee_type] = fee.amount

                row["amenities"] = ", ".join(a.amenity for a in rec.amenities)

                if rec.demographics:
                    row["median_age"] = rec.demographics.median_age
                    row["median_income"] = rec.demographics.median_household_income
                    row["owner_occupancy_pct"] = rec.demographics.owner_occupancy_pct
                    row["population"] = rec.demographics.population

                writer.writerow(row)

        logger.info("Exported %d communities to %s", len(records), output_path)
        return output_path

    def export_markdown(
        self,
        output_path: Path | str,
        records: Optional[list[CommunityRecord]] = None,
        discovered: Optional[list[dict[str, Any]]] = None,
    ) -> Path:
        """Export all communities to a Markdown document with a table.

        ``discovered`` optionally provides raw dicts (name, city, county,
        verified, confidence, source_url) for communities not yet in the
        database, so they still appear in the document.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if records is None:
            with self.session() as session:
                rows = session.query(CommunityRow).order_by(CommunityRow.name).all()
                records = [r.to_record() for r in rows]

        def esc(value: Any) -> str:
            if value is None or value == "":
                return "-"
            return str(value).replace("|", "\\|").replace("\n", " ")

        headers = [
            "Name", "City", "County", "Gated", "Verified", "Confidence", "Source",
            "Lat", "Lng",
            "HOA Monthly", "HOA Annual", "CDD Assessment",
            "Amenities", "Median Age", "Median Income", "Owner Occ %", "Population",
        ]

        lines = [
            f"# Communities Export — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
            "",
            f"Total communities: {len(records)}",
            "",
            "| " + " | ".join(headers) + " |",
            "|" + "|".join(["---"] * len(headers)) + "|",
        ]

        for rec in records:
            lat = lng = "-"
            if rec.identity.geo_point:
                lat = esc(rec.identity.geo_point.get("lat"))
                lng = esc(rec.identity.geo_point.get("lng"))

            fees = {f.fee_type: f.amount for f in rec.fees}
            demo = rec.demographics
            row = [
                esc(rec.identity.name),
                esc(rec.identity.city),
                esc(rec.identity.county_fips),
                "Yes" if rec.identity.is_gated else "No",
                "Yes",
                "-",
                "database",
                lat,
                lng,
                esc(fees.get("hoa_monthly")),
                esc(fees.get("hoa_annual")),
                esc(fees.get("cdd_assessment")),
                esc(", ".join(a.amenity for a in rec.amenities) or None),
                esc(demo.median_age if demo else None),
                esc(demo.median_household_income if demo else None),
                esc(demo.owner_occupancy_pct if demo else None),
                esc(demo.population if demo else None),
            ]
            lines.append("| " + " | ".join(row) + " |")

        if discovered:
            known = {slugify(rec.identity.name) for rec in records}
            seen = set(known)
            for d in discovered:
                name = d.get("name")
                if not name:
                    continue
                key = slugify(str(name))
                if key in seen:
                    continue
                seen.add(key)
                row = [
                    esc(name),
                    esc(d.get("city")),
                    esc(d.get("county")),
                    "Yes" if d.get("is_gated", True) else "No",
                    "Yes" if d.get("verified") else "No",
                    esc(d.get("confidence")),
                    esc(d.get("source_name") or d.get("source_url")),
                    "-", "-", "-", "-", "-", "-", "-", "-", "-", "-",
                ]
                lines.append("| " + " | ".join(row) + " |")

        total = len(lines) - 6  # minus title, blank, count, blank, header, separator
        lines[2] = f"Total communities: {total}"
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("Exported %d communities to %s", total, output_path)
        return output_path

    def summary(self) -> dict[str, Any]:
        with self.session() as session:
            total = session.query(func.count(CommunityRow.id)).scalar()
            gated = session.query(func.count(CommunityRow.id)).filter(CommunityRow.is_gated == True).scalar()
            with_fees = session.query(func.count(func.distinct(FeeRow.community_slug))).scalar()
            with_amenities = session.query(func.count(func.distinct(AmenityRow.community_slug))).scalar()
            with_demographics = session.query(func.count(func.count(CommunityRow.id))).filter(
                CommunityRow.slug.in_(
                    select(DemographicsRow.community_slug)
                )
            ).scalar() if False else session.query(func.count(func.distinct(DemographicsRow.community_slug))).scalar()

            sources = {}
            for source, cnt in session.query(CommunityRow.data_source, func.count(CommunityRow.id)).group_by(CommunityRow.data_source).all():
                sources[source or "unknown"] = cnt

            counties = {}
            for county, cnt in session.query(CommunityRow.county_fips, func.count(CommunityRow.id)).filter(CommunityRow.county_fips.isnot(None)).group_by(CommunityRow.county_fips).all():
                counties[county] = cnt

        return {
            "total_communities": total,
            "gated_count": gated,
            "with_fees": with_fees,
            "with_amenities": with_amenities,
            "with_demographics": with_demographics,
            "data_sources": sources,
            "by_county": counties,
        }
