"""File-based structured store for Workstream B (M1–M4; PostGIS migration is M5).

Layout::

    data/communities/
    ├── target_list.json             # seed catalog of communities to research
    ├── communities/<slug>.json      # one CommunityRecord per community
    └── research_log.jsonl           # append-only audit trail

Conflict policy (plan §5): never overwrite — all sourced facts are kept;
conflicting fee amounts / proximity names are surfaced as ``Discrepancy``
entries instead of being reconciled.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

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

DEFAULT_ROOT = Path("data") / "communities"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "unknown"


class CommunityStore:
    def __init__(self, root: Path | str = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        self.communities_dir = self.root / "communities"
        self.log_path = self.root / "research_log.jsonl"
        self.target_list_path = self.root / "target_list.json"

    # ── target list ────────────────────────────────────────────────────────

    def load_target_list(self) -> list[dict[str, Any]]:
        if not self.target_list_path.exists():
            return []
        data = json.loads(self.target_list_path.read_text(encoding="utf-8"))
        return data.get("communities", [])

    # ── community records ──────────────────────────────────────────────────

    def community_path(self, slug: str) -> Path:
        return self.communities_dir / f"{slug}.json"

    def load_community(self, slug: str) -> Optional[CommunityRecord]:
        path = self.community_path(slug)
        if not path.exists():
            return None
        return CommunityRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def upsert_identity(self, seed: dict[str, Any]) -> CommunityRecord:
        slug = slugify(seed["name"])
        existing = self.load_community(slug)
        if existing is not None:
            return existing
        return CommunityRecord(
            identity=CommunityIdentity(
                name=seed["name"],
                slug=slug,
                county_fips=seed.get("county_fips"),
                city=seed.get("city"),
                hoa_name=seed.get("hoa_name"),
                cdd_name=seed.get("cdd_name"),
                is_gated=seed.get("is_gated"),
                notes=seed.get("notes"),
            )
        )

    def save_community(self, record: CommunityRecord) -> Path:
        self.communities_dir.mkdir(parents=True, exist_ok=True)
        record.updated_at = utcnow()
        path = self.community_path(record.identity.slug)
        fd, tmp_name = tempfile.mkstemp(dir=self.communities_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(record.model_dump_json(indent=2))
            os.replace(tmp_name, path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        return path

    # ── research log ───────────────────────────────────────────────────────

    def append_research_log(self, entries: list[ResearchLogEntry]) -> None:
        if not entries:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(entry.model_dump_json() + "\n")

    def read_research_log(self) -> list[ResearchLogEntry]:
        if not self.log_path.exists():
            return []
        out = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(ResearchLogEntry.model_validate_json(line))
        return out


# ── merge / conflict logic (pure functions) ────────────────────────────────


def _fee_key(fee: FeeFact) -> tuple:
    return (fee.fee_type, fee.amount, fee.period or "", fee.source_url)


def _amenity_key(a: AmenityFact) -> tuple:
    return (a.amenity, a.detail or "", a.source_url)


def _proximity_key(p: ProximityMetric) -> tuple:
    return (p.category, p.nearest_name or "", round(p.distance_miles or 0.0, 2), p.source_url)


def detect_discrepancies(record: CommunityRecord) -> list[Discrepancy]:
    """Rescan stored facts; flag fields where sources disagree."""
    out: list[Discrepancy] = []

    fees_by_type: dict[str, list[FeeFact]] = {}
    for fee in record.fees:
        fees_by_type.setdefault(fee.fee_type, []).append(fee)
    for fee_type, fees in sorted(fees_by_type.items()):
        variants = {(f.amount, f.period or "") for f in fees}
        if len(variants) > 1:
            out.append(
                Discrepancy(
                    field=f"fees.{fee_type}",
                    values=[
                        {
                            "amount": f.amount,
                            "period": f.period,
                            "source_url": f.source_url,
                            "retrieved_at": f.retrieved_at.isoformat(),
                        }
                        for f in fees
                    ],
                )
            )

    prox_by_cat: dict[str, list[ProximityMetric]] = {}
    for metric in record.proximity:
        prox_by_cat.setdefault(metric.category, []).append(metric)
    for category, metrics in sorted(prox_by_cat.items()):
        names = {m.nearest_name for m in metrics if m.nearest_name}
        if len(names) > 1:
            out.append(
                Discrepancy(
                    field=f"proximity.{category}",
                    values=[
                        {
                            "nearest_name": m.nearest_name,
                            "distance_miles": m.distance_miles,
                            "source_url": m.source_url,
                        }
                        for m in metrics
                    ],
                )
            )
    return out


def merge_facts(record: CommunityRecord, facts: CommunityFacts) -> list[Discrepancy]:
    """Merge agent-proposed facts into ``record`` in place.

    Duplicates (same fact from same source) are dropped; conflicting facts are
    kept and surfaced via ``record.discrepancies``. Returns the updated
    discrepancy list.
    """
    fee_keys = {_fee_key(f) for f in record.fees}
    for fee in facts.fees:
        key = _fee_key(fee)
        if key not in fee_keys:
            record.fees.append(fee)
            fee_keys.add(key)

    amenity_keys = {_amenity_key(a) for a in record.amenities}
    for amenity in facts.amenities:
        key = _amenity_key(amenity)
        if key not in amenity_keys:
            record.amenities.append(amenity)
            amenity_keys.add(key)

    if facts.demographics is not None:
        incoming = facts.demographics
        current = record.demographics
        if current is None or incoming.retrieved_at >= current.retrieved_at:
            if current is not None:
                record.demographics_history.append(current)
            record.demographics = incoming
        else:
            record.demographics_history.append(incoming)

    proximity_keys = {_proximity_key(p) for p in record.proximity}
    for metric in facts.proximity:
        key = _proximity_key(metric)
        if key not in proximity_keys:
            record.proximity.append(metric)
            proximity_keys.add(key)

    for question in facts.open_questions:
        text = question.strip()
        if text and text not in record.open_questions:
            record.open_questions.append(text)

    record.discrepancies = detect_discrepancies(record)
    return record.discrepancies
