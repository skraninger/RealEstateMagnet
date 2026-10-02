"""
Raw storage layout + manifest.

  data/raw/
  ├── manifest.json              # one entry per fetch
  └── <source-slug>/
      └── <dataset-slug>.<ext>   # verbatim bytes from the source

Idempotent re-runs: a (source, dataset) already present in the manifest is
skipped unless ``force=True``.  ZIP/SHP are kept unextracted — Phase 3
decides extraction.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .fetchers.base import FetchResult

MANIFEST_NAME = "manifest.json"

_FORMAT_EXT = {
    "csv": "csv", "tsv": "tsv", "json": "json", "geojson": "json",
    "zip": "zip", "shp": "shp", "xlsx": "xlsx", "pdf": "pdf",
    "html": "html", "xml": "xml", "kml": "kml",
}


def slugify(name: str, max_len: int = 60) -> str:
    """Filesystem-safe slug: lowercase alnum/dash, no leading dot."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return (text or "unnamed")[:max_len]


def extension_for(format: str) -> str:
    return _FORMAT_EXT.get(format, "bin")


class RawStore:
    """Writes fetched datasets to disk and maintains manifest.json."""

    def __init__(self, root: Path | str = Path("data") / "raw") -> None:
        self.root = Path(root)
        (self.root).mkdir(parents=True, exist_ok=True)
        self._manifest_path = self.root / MANIFEST_NAME
        self._manifest: list[dict[str, Any]] = self._load()

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------

    def _load(self) -> list[dict[str, Any]]:
        if self._manifest_path.exists():
            try:
                data = json.loads(self._manifest_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def save_manifest(self) -> None:
        self._manifest_path.write_text(
            json.dumps(self._manifest, indent=2), encoding="utf-8"
        )

    @property
    def manifest(self) -> list[dict[str, Any]]:
        return self._manifest

    def has_entry(self, source_name: str, dataset_id: str) -> bool:
        for entry in self._manifest:
            if entry.get("source") == source_name and entry.get("dataset_id") == dataset_id:
                return True
        return False

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def write(self, result: FetchResult) -> Path:
        """Persist one FetchResult; returns the file path. Updates manifest."""
        source_dir = self.root / slugify(result.source_name)
        source_dir.mkdir(parents=True, exist_ok=True)

        ext = extension_for(result.format)
        path = source_dir / f"{slugify(result.dataset_id)}.{ext}"

        if result.raw_bytes is not None:
            payload = result.raw_bytes
        elif result.records is not None:
            payload = json.dumps(result.records, indent=1).encode("utf-8")
        else:
            raise ValueError(f"FetchResult for {result.dataset_id} has no data to write")

        path.write_bytes(payload)

        entry = {
            "source": result.source_name,
            "dataset_id": result.dataset_id,
            "name": result.metadata.get("name", result.dataset_id),
            "url": result.url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "format": result.format,
            "row_count": result.row_count,
            "size_bytes": len(payload),
            "path": str(path.relative_to(self.root)),
            "warnings": result.warnings,
        }
        self._manifest = [
            e for e in self._manifest
            if not (e.get("source") == result.source_name and e.get("dataset_id") == result.dataset_id)
        ]
        self._manifest.append(entry)
        self.save_manifest()
        return path
