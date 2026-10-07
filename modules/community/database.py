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
from sqlalchemy.exc import IntegrityError
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
    
    # Data quality metrics
    has_community_data = Column(Boolean, default=False, index=True)
    data_quality_score = Column(Float, default=0.0, index=True)
    data_types_found = Column(String(255))  # comma-separated: fees,amenities,demographics,proximity
    data_summary = Column(Text)  # brief summary of extracted data


# Pipeline status values (community_pipeline_status.status)
PIPELINE_STATUSES = ("pending", "processing", "partial", "completed", "failed")

# Condenser run status values (community_condenser_runs.status)
CONDENSER_RUN_STATUSES = ("pending", "running", "done", "error", "skipped")

# Statuses that count as a did-not-fail, "handled" condenser run
TERMINAL_OK_STATUSES = ("done", "skipped")

# Statuses that count as any terminal (handled) condenser run
TERMINAL_STATUSES = ("done", "error", "skipped")


class CommunityPipelineStatusRow(Base):
    """Per-community pipeline progress — the source of truth for resumability.

    A community is processed as a unit: it is flagged ``processing`` while its
    selected condensers run, then ``completed`` once every selected condenser
    has finished (``done`` or ``skipped``). A restart selects the first
    community whose status is not ``completed`` (ordered by ``sort_order``).
    """

    __tablename__ = "community_pipeline_status"

    community_slug = Column(
        String(255), ForeignKey("communities.slug"), primary_key=True
    )
    status = Column(String(20), nullable=False, default="pending", index=True)
    sort_order = Column(Integer, default=0, index=True)
    attempts = Column(Integer, default=0)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    last_heartbeat = Column(DateTime)
    last_error = Column(Text)
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class CommunityCondenserRunRow(Base):
    """One row per (community, condenser) step — replaces pipeline_state.json."""

    __tablename__ = "community_condenser_runs"
    __table_args__ = (
        UniqueConstraint("community_slug", "condenser", name="uq_condenser_run"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    community_slug = Column(
        String(255), ForeignKey("communities.slug"), nullable=False, index=True
    )
    condenser = Column(String(30), nullable=False, index=True)
    status = Column(String(20), default="pending", index=True)
    elapsed = Column(Float, default=0.0)
    fees = Column(Integer, default=0)
    amenities = Column(Integer, default=0)
    proximity = Column(Integer, default=0)
    demographics_present = Column(Boolean, default=False)
    sources_consulted = Column(Integer, default=0)
    errors = Column(Text)  # JSON-encoded list[str]
    started_at = Column(DateTime)
    finished_at = Column(DateTime)


class CommunityURLRow(Base):
    """Many-to-many link between a community and an inspected source URL.

    ``source_urls`` remains the canonical URL registry (deduplicated by URL);
    this table records that a URL was discovered/inspected *for a community*,
    so the same URL can be attributed to multiple communities.
    """

    __tablename__ = "community_urls"
    __table_args__ = (
        UniqueConstraint("community_slug", "url_id", name="uq_community_url"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    community_slug = Column(
        String(255), ForeignKey("communities.slug"), nullable=False, index=True
    )
    url_id = Column(Integer, ForeignKey("source_urls.id"), nullable=False, index=True)
    discovered_by = Column(String(50), index=True)
    first_seen_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen_at = Column(DateTime)
    inspected = Column(Boolean, default=False, index=True)
    status = Column(String(30), index=True)
    has_community_data = Column(Boolean, default=False)
    data_quality_score = Column(Float, default=0.0)


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
            # First check by slug (the actual UNIQUE constraint)
            existing = session.query(CommunityRow).filter(
                CommunityRow.slug == record.identity.slug
            ).first()
            
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
            try:
                session.commit()
            except IntegrityError:
                # A row with this slug already exists (e.g. a concurrent / prior
                # insert). Roll back and update the existing row instead.
                session.rollback()
                existing = session.query(CommunityRow).filter(
                    CommunityRow.slug == record.identity.slug
                ).first()
                if existing is None:
                    raise
                self._update_row(existing, record, session)
                session.commit()
                return existing.slug
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

    # ── community registration & pipeline status ────────────────────────────

    def register_community(
        self,
        *,
        name: str,
        slug: str,
        city: Optional[str] = None,
        county: Optional[str] = None,
        source: str = "pipeline",
        sort_order: Optional[int] = None,
    ) -> str:
        """Get-or-create a community row and its pipeline status row.

        Duplicate-safe: if the slug already exists it is updated (filling only
        missing city/county) rather than inserted again.
        """
        with self.session() as session:
            row = session.query(CommunityRow).filter(CommunityRow.slug == slug).first()
            if row is None:
                row = CommunityRow(
                    id=str(uuid.uuid4()),
                    name=name,
                    slug=slug,
                    city=city,
                    county_fips=county,
                    is_gated=True,
                    data_source=source,
                )
                session.add(row)
                try:
                    session.flush()
                except IntegrityError:
                    session.rollback()
                    row = session.query(CommunityRow).filter(
                        CommunityRow.slug == slug
                    ).first()
            else:
                if city and not row.city:
                    row.city = city
                if county and not row.county_fips:
                    row.county_fips = county

            status_row = session.query(CommunityPipelineStatusRow).filter_by(
                community_slug=slug
            ).first()
            if status_row is None:
                status_row = CommunityPipelineStatusRow(
                    community_slug=slug,
                    status="pending",
                    sort_order=sort_order if sort_order is not None else 0,
                )
                session.add(status_row)
            elif sort_order is not None and not status_row.sort_order:
                status_row.sort_order = sort_order

            session.commit()
            return slug

    def register_communities(self, infos: list[dict[str, Any]]) -> int:
        """Register many communities, assigning ``sort_order`` by list position.

        ``infos`` items accept keys: ``name``, ``slug`` (optional), ``city``,
        ``county``, ``source``, ``is_gated``. Existing communities keep their
        original ``sort_order``.
        """
        registered = 0
        with self.session() as session:
            for idx, info in enumerate(infos):
                name = info["name"]
                slug = info.get("slug") or slugify(name)
                row = session.query(CommunityRow).filter(
                    CommunityRow.slug == slug
                ).first()
                city = info.get("city")
                county = info.get("county")
                if row is None:
                    row = CommunityRow(
                        id=str(uuid.uuid4()),
                        name=name,
                        slug=slug,
                        city=city,
                        county_fips=county,
                        is_gated=info.get("is_gated", True),
                        data_source=info.get("source", "pipeline"),
                    )
                    session.add(row)
                else:
                    if city and not row.city:
                        row.city = city
                    if county and not row.county_fips:
                        row.county_fips = county

                status_row = session.query(CommunityPipelineStatusRow).filter_by(
                    community_slug=slug
                ).first()
                if status_row is None:
                    session.add(
                        CommunityPipelineStatusRow(
                            community_slug=slug,
                            status="pending",
                            sort_order=idx,
                        )
                    )
                registered += 1
            session.commit()
        return registered

    def assign_sort_order(self, ordered_slugs: list[str]) -> int:
        """Set ``sort_order`` for communities from an ordered slug list."""
        with self.session() as session:
            for idx, slug in enumerate(ordered_slugs):
                row = session.query(CommunityPipelineStatusRow).filter_by(
                    community_slug=slug
                ).first()
                if row is None:
                    session.add(
                        CommunityPipelineStatusRow(
                            community_slug=slug, status="pending", sort_order=idx
                        )
                    )
                else:
                    row.sort_order = idx
            session.commit()
        return len(ordered_slugs)

    def count_condenser_runs(self) -> int:
        with self.session() as session:
            return session.query(func.count(CommunityCondenserRunRow.id)).scalar() or 0

    def get_pipeline_status(self, slug: str) -> Optional[str]:
        with self.session() as session:
            row = session.query(CommunityPipelineStatusRow).filter_by(
                community_slug=slug
            ).first()
            return row.status if row else None

    def list_pipeline_statuses(self) -> list[dict[str, Any]]:
        """Return every community's pipeline status in processing order."""
        with self.session() as session:
            rows = (
                session.query(CommunityPipelineStatusRow)
                .order_by(
                    CommunityPipelineStatusRow.sort_order,
                    CommunityPipelineStatusRow.community_slug,
                )
                .all()
            )
            out: list[dict[str, Any]] = []
            for r in rows:
                c = session.query(CommunityRow).filter_by(
                    slug=r.community_slug
                ).first()
                out.append(
                    {
                        "slug": r.community_slug,
                        "name": c.name if c else r.community_slug,
                        "city": c.city if c else None,
                        "status": r.status,
                        "sort_order": r.sort_order,
                        "attempts": r.attempts,
                        "started_at": r.started_at,
                        "completed_at": r.completed_at,
                        "last_error": r.last_error,
                    }
                )
            return out

    def next_incomplete_community(self) -> Optional[str]:
        """First community (by sort_order) whose status is not 'completed'."""
        with self.session() as session:
            row = (
                session.query(CommunityPipelineStatusRow)
                .filter(CommunityPipelineStatusRow.status != "completed")
                .order_by(
                    CommunityPipelineStatusRow.sort_order,
                    CommunityPipelineStatusRow.community_slug,
                )
                .first()
            )
            return row.community_slug if row else None

    def list_incomplete_communities(self) -> list[str]:
        with self.session() as session:
            rows = (
                session.query(CommunityPipelineStatusRow.community_slug)
                .filter(CommunityPipelineStatusRow.status != "completed")
                .order_by(
                    CommunityPipelineStatusRow.sort_order,
                    CommunityPipelineStatusRow.community_slug,
                )
                .all()
            )
            return [r[0] for r in rows]

    def _status_row(self, session: Session, slug: str) -> CommunityPipelineStatusRow:
        row = session.query(CommunityPipelineStatusRow).filter_by(
            community_slug=slug
        ).first()
        if row is None:
            row = CommunityPipelineStatusRow(community_slug=slug, status="pending")
            session.add(row)
        return row

    def mark_processing(self, slug: str) -> None:
        now = datetime.now(timezone.utc)
        with self.session() as session:
            row = self._status_row(session, slug)
            row.status = "processing"
            if row.started_at is None:
                row.started_at = now
            row.last_heartbeat = now
            row.attempts = (row.attempts or 0) + 1
            session.commit()

    def mark_completed(self, slug: str) -> None:
        now = datetime.now(timezone.utc)
        with self.session() as session:
            row = self._status_row(session, slug)
            row.status = "completed"
            row.completed_at = now
            row.last_heartbeat = now
            row.last_error = None
            session.commit()

    def mark_partial(self, slug: str, error: Optional[str] = None) -> None:
        with self.session() as session:
            row = self._status_row(session, slug)
            row.status = "partial"
            if error:
                row.last_error = error
            session.commit()

    def mark_failed(self, slug: str, error: Optional[str] = None) -> None:
        with self.session() as session:
            row = self._status_row(session, slug)
            row.status = "failed"
            if error:
                row.last_error = error
            session.commit()

    def reset_stale_processing(self) -> int:
        """Convert interrupted 'processing' communities to 'partial' (resumable)."""
        with self.session() as session:
            rows = (
                session.query(CommunityPipelineStatusRow)
                .filter(CommunityPipelineStatusRow.status == "processing")
                .all()
            )
            for row in rows:
                row.status = "partial"
            session.commit()
            return len(rows)

    def reset_all_statuses(self) -> int:
        """Reset every community to 'pending' (used by --reset); data is kept."""
        with self.session() as session:
            rows = session.query(CommunityPipelineStatusRow).all()
            for row in rows:
                row.status = "pending"
                row.started_at = None
                row.completed_at = None
                row.last_heartbeat = None
                row.last_error = None
                row.attempts = 0
            session.commit()
            return len(rows)

    def reset_condenser_runs(self, slugs: Optional[list[str]] = None) -> int:
        """Reset condenser run rows back to 'pending'."""
        with self.session() as session:
            q = session.query(CommunityCondenserRunRow)
            if slugs:
                q = q.filter(CommunityCondenserRunRow.community_slug.in_(slugs))
            rows = q.all()
            for row in rows:
                row.status = "pending"
                row.started_at = None
                row.finished_at = None
                row.errors = None
            session.commit()
            return len(rows)

    # ── condenser runs ──────────────────────────────────────────────────────

    def record_condenser_run(self, slug: str, condenser: str, result: Any) -> None:
        """Upsert the run row for ``(slug, condenser)`` from a step result."""
        status = getattr(result, "status", "pending")
        errors = getattr(result, "errors", None)
        with self.session() as session:
            row = session.query(CommunityCondenserRunRow).filter_by(
                community_slug=slug, condenser=condenser
            ).first()
            now = datetime.now(timezone.utc)
            if row is None:
                row = CommunityCondenserRunRow(
                    community_slug=slug, condenser=condenser
                )
                session.add(row)
            row.status = status
            row.elapsed = getattr(result, "elapsed", 0.0) or 0.0
            row.fees = getattr(result, "fees", 0) or 0
            row.amenities = getattr(result, "amenities", 0) or 0
            row.proximity = getattr(result, "proximity", 0) or 0
            row.demographics_present = bool(
                getattr(result, "demographics_present", False)
            )
            row.sources_consulted = getattr(result, "sources_consulted", 0) or 0
            if errors is not None:
                row.errors = json.dumps(list(errors))
            if status == "running":
                row.started_at = now
            elif status in TERMINAL_STATUSES:
                row.finished_at = now
            session.commit()

    def get_condenser_runs(self, slug: str) -> dict[str, Any]:
        with self.session() as session:
            rows = session.query(CommunityCondenserRunRow).filter_by(
                community_slug=slug
            ).all()
            return {
                r.condenser: {
                    "status": r.status,
                    "elapsed": r.elapsed,
                    "fees": r.fees,
                    "amenities": r.amenities,
                    "proximity": r.proximity,
                    "demographics_present": r.demographics_present,
                    "sources_consulted": r.sources_consulted,
                    "errors": json.loads(r.errors) if r.errors else [],
                }
                for r in rows
            }

    def condenser_statuses(self, slug: str) -> dict[str, str]:
        with self.session() as session:
            rows = session.query(CommunityCondenserRunRow).filter_by(
                community_slug=slug
            ).all()
            return {r.condenser: r.status for r in rows}

    def community_is_complete(self, slug: str, condensers: list[str]) -> bool:
        """True when every selected condenser has finished (done/skipped)."""
        statuses = self.condenser_statuses(slug)
        return all(statuses.get(c) in TERMINAL_OK_STATUSES for c in condensers)

    # ── URL ↔ community links ───────────────────────────────────────────────

    def link_url_to_community(
        self,
        slug: str,
        url_id: int,
        discovered_by: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.session() as session:
            row = session.query(CommunityURLRow).filter_by(
                community_slug=slug, url_id=url_id
            ).first()
            if row is None:
                session.add(
                    CommunityURLRow(
                        community_slug=slug,
                        url_id=url_id,
                        discovered_by=discovered_by,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
            else:
                row.last_seen_at = now
                if discovered_by and not row.discovered_by:
                    row.discovered_by = discovered_by
            session.commit()

    def update_community_url(self, slug: str, url_id: int, **fields: Any) -> None:
        with self.session() as session:
            row = session.query(CommunityURLRow).filter_by(
                community_slug=slug, url_id=url_id
            ).first()
            if row is None:
                row = CommunityURLRow(community_slug=slug, url_id=url_id)
                session.add(row)
            for key, value in fields.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            row.last_seen_at = datetime.now(timezone.utc)
            session.commit()

    def get_urls_for_community(self, slug: str) -> list[dict[str, Any]]:
        with self.session() as session:
            rows = (
                session.query(CommunityURLRow, SourceURLRow)
                .join(SourceURLRow, CommunityURLRow.url_id == SourceURLRow.id)
                .filter(CommunityURLRow.community_slug == slug)
                .order_by(CommunityURLRow.last_seen_at.desc())
                .all()
            )
            return [
                {
                    "url": su.url,
                    "domain": su.domain,
                    "title": su.title,
                    "discovered_by": cu.discovered_by,
                    "status": cu.status or su.status,
                    "inspected": cu.inspected,
                    "has_community_data": cu.has_community_data,
                    "data_quality_score": cu.data_quality_score,
                    "first_seen_at": cu.first_seen_at,
                    "last_seen_at": cu.last_seen_at,
                }
                for cu, su in rows
            ]

    def get_communities_for_url(self, url: str) -> list[str]:
        with self.session() as session:
            rows = (
                session.query(CommunityURLRow.community_slug)
                .join(SourceURLRow, CommunityURLRow.url_id == SourceURLRow.id)
                .filter(SourceURLRow.url == url)
                .all()
            )
            return [r[0] for r in rows]

    def backfill_community_urls(self) -> int:
        """Populate ``community_urls`` from legacy ``source_urls.community_slug``."""
        count = 0
        with self.session() as session:
            srcs = (
                session.query(SourceURLRow)
                .filter(
                    SourceURLRow.community_slug.isnot(None),
                    SourceURLRow.community_slug != "",
                )
                .all()
            )
            now = datetime.now(timezone.utc)
            for src in srcs:
                exists = session.query(CommunityURLRow).filter_by(
                    community_slug=src.community_slug, url_id=src.id
                ).first()
                if exists:
                    continue
                session.add(
                    CommunityURLRow(
                        community_slug=src.community_slug,
                        url_id=src.id,
                        discovered_by=src.discovered_by,
                        first_seen_at=src.discovered_at or now,
                        last_seen_at=src.accessed_at
                        or src.discovered_at
                        or now,
                        inspected=bool(src.status and src.status != "pending"),
                        status=src.status,
                        has_community_data=bool(src.has_community_data),
                        data_quality_score=src.data_quality_score or 0.0,
                    )
                )
                count += 1
            session.commit()
        return count

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

            pipeline_statuses: dict[str, int] = {}
            for status, cnt in session.query(
                CommunityPipelineStatusRow.status,
                func.count(CommunityPipelineStatusRow.community_slug),
            ).group_by(CommunityPipelineStatusRow.status).all():
                pipeline_statuses[status or "unknown"] = cnt

            linked_urls = (
                session.query(func.count(func.distinct(CommunityURLRow.url_id))).scalar()
                or 0
            )
            community_url_links = (
                session.query(func.count(CommunityURLRow.id)).scalar() or 0
            )

        return {
            "total_communities": total,
            "gated_count": gated,
            "with_fees": with_fees,
            "with_amenities": with_amenities,
            "with_demographics": with_demographics,
            "data_sources": sources,
            "by_county": counties,
            "pipeline_statuses": pipeline_statuses,
            "linked_urls": linked_urls,
            "community_url_links": community_url_links,
        }
