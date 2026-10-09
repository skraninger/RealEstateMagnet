"""Offline tests for the web viewer templates and write endpoints.

The viewer depends on the web extras (fastapi/starlette/jinja2). Jinja2 is
imported lazily via ``pytest.importorskip`` so the core suite still runs in an
environment that hasn't installed them.
"""

from __future__ import annotations

import sqlite3
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


class TestViewerWriteEndpoints:
    """Inline edit/delete endpoints exercised against a temp SQLite DB."""

    @pytest.fixture()
    def env(self, tmp_path, monkeypatch):
        pytest.importorskip("fastapi")
        pytest.importorskip("httpx")
        from fastapi.testclient import TestClient

        import modules.web.viewer as viewer
        from modules.community.database import CommunityDatabase

        db_path = tmp_path / "communities.db"
        db = CommunityDatabase(db_path)
        db.create_tables()
        db.register_community(
            name="Test Community", slug="test-community", city="Naples", county="collier"
        )

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO community_fees (community_slug, fee_type, amount, currency, confidence)"
            " VALUES (?,?,?,?,?)",
            ("test-community", "hoa_monthly", 100.0, "USD", 0.9),
        )
        conn.execute(
            "INSERT INTO community_amenities (community_slug, amenity, confidence) VALUES (?,?,?)",
            ("test-community", "Pool", 0.8),
        )
        conn.execute(
            "INSERT INTO community_demographics (community_slug, median_age, population)"
            " VALUES (?,?,?)",
            ("test-community", 55.0, 1000),
        )
        conn.execute(
            "INSERT INTO proximity_metrics (community_slug, category, nearest_name, distance_miles)"
            " VALUES (?,?,?,?)",
            ("test-community", "beach", "X", 1.5),
        )
        conn.execute(
            "INSERT INTO community_condenser_runs (community_slug, condenser, status)"
            " VALUES (?,?,?)",
            ("test-community", "ai", "done"),
        )
        conn.execute(
            "INSERT INTO source_urls (url, domain, status) VALUES (?,?,?)",
            ("https://example.com/a", "example.com", "printed"),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(viewer, "DB_PATH", db_path)
        return TestClient(viewer.app), db_path

    @staticmethod
    def _scalar(db_path, sql, params=()):
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(sql, params).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def test_detail_page_offers_edit_and_delete(self, env):
        client, _ = env
        r = client.get("/communities/test-community")
        assert r.status_code == 200
        assert "Edit" in r.text
        assert "Delete" in r.text

    def test_edit_then_delete_fee(self, env):
        client, db_path = env
        fee_id = self._scalar(db_path, "SELECT id FROM community_fees LIMIT 1")
        r = client.post(
            f"/communities/test-community/facts/fees/{fee_id}/edit",
            data={
                "fee_type": "hoa_annual",
                "amount": "1200",
                "period": "year",
                "currency": "USD",
                "source_url": "https://example.com",
                "confidence": "0.7",
                "note": "updated",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        conn = sqlite3.connect(db_path)
        fee_type, amount, note = conn.execute(
            "SELECT fee_type, amount, note FROM community_fees WHERE id=?", (fee_id,)
        ).fetchone()
        conn.close()
        assert fee_type == "hoa_annual"
        assert amount == 1200.0
        assert note == "updated"

        r = client.post(
            f"/communities/test-community/facts/fees/{fee_id}/delete",
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert self._scalar(db_path, "SELECT COUNT(*) FROM community_fees WHERE id=?", (fee_id,)) == 0

    def test_blank_required_field_is_left_unchanged(self, env):
        client, db_path = env
        fee_id = self._scalar(db_path, "SELECT id FROM community_fees LIMIT 1")
        r = client.post(
            f"/communities/test-community/facts/fees/{fee_id}/edit",
            data={"fee_type": "", "amount": "250"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        conn = sqlite3.connect(db_path)
        fee_type, amount = conn.execute(
            "SELECT fee_type, amount FROM community_fees WHERE id=?", (fee_id,)
        ).fetchone()
        conn.close()
        assert fee_type == "hoa_monthly"  # unchanged
        assert amount == 250.0

    def test_edit_and_delete_pipeline_status(self, env):
        client, db_path = env
        r = client.post(
            "/communities/test-community/pipeline/edit",
            data={"status": "completed", "attempts": "3", "sort_order": "7", "last_error": ""},
            follow_redirects=False,
        )
        assert r.status_code == 303
        conn = sqlite3.connect(db_path)
        status, attempts, sort_order = conn.execute(
            "SELECT status, attempts, sort_order FROM community_pipeline_status"
            " WHERE community_slug='test-community'"
        ).fetchone()
        conn.close()
        assert (status, attempts, sort_order) == ("completed", 3, 7)

        r = client.post(
            "/communities/test-community/pipeline/delete", follow_redirects=False
        )
        assert r.status_code == 303
        assert (
            self._scalar(
                db_path,
                "SELECT COUNT(*) FROM community_pipeline_status WHERE community_slug='test-community'",
            )
            == 0
        )

    def test_invalid_enum_value_is_ignored(self, env):
        client, db_path = env
        client.post(
            "/communities/test-community/pipeline/edit",
            data={"status": "not-a-status", "attempts": "2"},
            follow_redirects=False,
        )
        conn = sqlite3.connect(db_path)
        status, attempts = conn.execute(
            "SELECT status, attempts FROM community_pipeline_status"
            " WHERE community_slug='test-community'"
        ).fetchone()
        conn.close()
        assert status == "pending"  # unchanged
        assert attempts == 2  # other fields still applied

    def test_edit_condenser_run_validates_errors_json(self, env):
        client, db_path = env
        run_id = self._scalar(db_path, "SELECT id FROM community_condenser_runs LIMIT 1")

        r = client.post(
            f"/communities/test-community/condenser-runs/{run_id}/edit",
            data={"status": "done", "fees": "4", "errors": '["boom"]'},
            follow_redirects=False,
        )
        assert r.status_code == 303
        conn = sqlite3.connect(db_path)
        fees, errors = conn.execute(
            "SELECT fees, errors FROM community_condenser_runs WHERE id=?", (run_id,)
        ).fetchone()
        conn.close()
        assert fees == 4 and errors == '["boom"]'

        # Invalid JSON is ignored, but the other fields still apply.
        client.post(
            f"/communities/test-community/condenser-runs/{run_id}/edit",
            data={"status": "done", "fees": "9", "errors": "not json"},
            follow_redirects=False,
        )
        conn = sqlite3.connect(db_path)
        fees, errors = conn.execute(
            "SELECT fees, errors FROM community_condenser_runs WHERE id=?", (run_id,)
        ).fetchone()
        conn.close()
        assert fees == 9 and errors == '["boom"]'

    def test_delete_condenser_run(self, env):
        client, db_path = env
        run_id = self._scalar(db_path, "SELECT id FROM community_condenser_runs LIMIT 1")
        r = client.post(
            f"/communities/test-community/condenser-runs/{run_id}/delete",
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert self._scalar(db_path, "SELECT COUNT(*) FROM community_condenser_runs") == 0

    def test_edit_and_delete_source_url(self, env):
        client, db_path = env
        url_id = self._scalar(db_path, "SELECT id FROM source_urls LIMIT 1")
        # Link it to the community so the delete path has a join row to clean up.
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO community_urls (community_slug, url_id) VALUES (?,?)",
            ("test-community", url_id),
        )
        conn.commit()
        conn.close()

        r = client.post(
            f"/urls/{url_id}/edit",
            data={
                "title": "Example",
                "domain": "example.com",
                "status": "reviewed",
                "data_quality_score": "82.5",
                "has_community_data": "on",
                "reviewed": "on",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        conn = sqlite3.connect(db_path)
        title, status, quality, has_data, reviewed = conn.execute(
            "SELECT title, status, data_quality_score, has_community_data, reviewed"
            " FROM source_urls WHERE id=?",
            (url_id,),
        ).fetchone()
        conn.close()
        assert (title, status, quality, has_data, reviewed) == ("Example", "reviewed", 82.5, 1, 1)

        r = client.post(f"/urls/{url_id}/delete", follow_redirects=False)
        assert r.status_code == 303
        assert self._scalar(db_path, "SELECT COUNT(*) FROM source_urls") == 0
        assert self._scalar(db_path, "SELECT COUNT(*) FROM community_urls") == 0

    def test_unknown_table_and_missing_row_return_404(self, env):
        client, _ = env
        assert client.post("/communities/test-community/facts/bogus/1/edit").status_code == 404
        assert client.post("/communities/test-community/facts/fees/999999/edit").status_code == 404

    def test_edit_mode_renders_inputs_and_form(self, env):
        client, db_path = env
        fee_id = self._scalar(db_path, "SELECT id FROM community_fees LIMIT 1")
        run_id = self._scalar(db_path, "SELECT id FROM community_condenser_runs LIMIT 1")

        r = client.get(f"/communities/test-community?edit_table=fees&edit_id={fee_id}")
        assert r.status_code == 200
        assert f'id="fees-edit-{fee_id}"' in r.text
        assert f'name="fee_type"' in r.text
        assert "Save" in r.text and "Cancel" in r.text

        r = client.get(
            f"/communities/test-community?edit_table=condenser_runs&edit_id={run_id}"
        )
        assert r.status_code == 200
        assert f'id="condenser_runs-edit-{run_id}"' in r.text

        r = client.get("/communities/test-community?edit_table=pipeline&edit_id=0")
        assert r.status_code == 200
        assert 'id="pipeline-edit-0"' in r.text

    def test_url_edit_mode_renders_fields(self, env):
        client, db_path = env
        url_id = self._scalar(db_path, "SELECT id FROM source_urls LIMIT 1")
        r = client.get(f"/urls?edit_table=urls&edit_id={url_id}")
        assert r.status_code == 200
        assert 'name="review_note"' in r.text
        assert 'name="data_summary"' in r.text
        assert f'action="/urls/{url_id}/edit"' in r.text
