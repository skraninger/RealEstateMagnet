"""Offline tests for the web viewer templates.

The viewer depends on the web extras (fastapi/starlette/jinja2). Jinja2 is
imported lazily via ``pytest.importorskip`` so the core suite still runs in an
environment that hasn't installed them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "modules" / "web" / "templates"


def _render(template_name: str, **context) -> str:
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)))
    # The detail template dereferences community.* unconditionally; a minimal
    # (empty) mapping is enough for the other fields to fall back to '-'/''.
    context.setdefault("community", {})
    return env.get_template(template_name).render(**context)


class TestCommunityDetailTemplate:
    def test_renders_without_any_facts(self):
        """A community with no child rows must still render (empty sections)."""
        html = _render("community_detail.html")
        assert "Back to Communities" in html

    def test_median_income_uses_thousands_separator(self):
        """Regression: Jinja's ``format`` filter is printf-style, so
        ``"{:,.0f}"|format(x)`` raised ``TypeError: not all arguments converted``
        whenever a community actually had a median household income."""
        html = _render(
            "community_detail.html",
            demographics=[{"median_household_income": 123456}],
        )
        assert "$123,456" in html

    def test_confidences_and_distances_render(self):
        """Format filters on the fact tables must not blow up on real values."""
        html = _render(
            "community_detail.html",
            fees=[{"fee_type": "hoa_monthly", "amount": 450.0, "confidence": 0.9}],
            amenities=[{"amenity": "Pool", "confidence": 0.8}],
            proximity=[{"category": "beach", "nearest_name": "X", "distance_miles": 1.5}],
        )
        assert "$450.00" in html
        assert "90%" in html
        assert "1.50" in html
