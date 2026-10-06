"""Export community data to Markdown (or CSV).

Usage::

    python -m modules.community.export --format markdown --output Documents/communities.md
    python -m modules.community.export --format csv --output data/communities.csv
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Optional

from .database import CommunityDatabase, DEFAULT_DB_PATH

logger = logging.getLogger(__name__)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Export community data to Markdown or CSV")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH, help="Path to communities.db")
    parser.add_argument("--format", choices=["markdown", "csv"], default="markdown")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("Documents") / "communities.md",
        help="Output file path",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db = CommunityDatabase(args.db_path)
    db.create_tables()

    discovered: list[dict] = []
    discovery_state = Path("data") / "communities" / "discovery_state.json"
    if discovery_state.exists():
        import json

        state = json.loads(discovery_state.read_text(encoding="utf-8"))
        discovered = list(state.get("discovered_communities", []))
        logger.info("Loaded %d discovered communities from %s", len(discovered), discovery_state)

    if args.format == "markdown":
        out = db.export_markdown(args.output, discovered=discovered)
    else:
        out = db.export_csv(args.output)

    print(f"Wrote export of {db.count()} database records (+{len(discovered)} discovered) to {out}")


if __name__ == "__main__":
    main()
