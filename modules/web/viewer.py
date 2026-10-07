"""FastAPI web viewer for RealEstateMagnet database.

Run with: python -m modules.web.viewer
Then open: http://localhost:8000
"""

import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="RealEstateMagnet Database Viewer")

# Setup templates - use absolute path based on this file's location
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Database path
DB_PATH = Path("data/communities.db")


def _ensure_schema() -> None:
    """Create the new tables and run the (idempotent) migration on startup.

    Best-effort: if the community package or legacy data is unavailable the
    viewer still serves whatever exists.
    """
    try:
        from modules.community.database import CommunityDatabase
        from modules.community.migration import migrate_database

        db = CommunityDatabase(DB_PATH)
        db.create_tables()
        migrate_database(db=db)
    except Exception:  # pragma: no cover - defensive
        pass


_ensure_schema()


def get_db():
    """Get database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Dashboard with overview statistics."""
    conn = get_db()
    cursor = conn.cursor()
    
    # Community statistics
    cursor.execute("SELECT COUNT(*) FROM communities")
    total_communities = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM communities WHERE is_gated = 1")
    gated_communities = cursor.fetchone()[0]
    
    # URL tracking statistics
    try:
        cursor.execute("SELECT COUNT(*) FROM source_urls")
        total_urls = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM source_urls WHERE status = 'printed'")
        printed_urls = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM source_urls WHERE status = 'failed_4xx'")
        failed_urls = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM source_urls WHERE has_community_data = 1")
        urls_with_data = cursor.fetchone()[0]
        
        cursor.execute("SELECT AVG(data_quality_score) FROM source_urls WHERE has_community_data = 1")
        avg_quality = cursor.fetchone()[0] or 0
        
        cursor.execute("SELECT COUNT(*) FROM source_urls WHERE data_quality_score >= 75")
        high_quality_count = cursor.fetchone()[0]
        
        # Quality distribution
        cursor.execute("""
            SELECT 
                CASE 
                    WHEN data_quality_score >= 75 THEN 'high'
                    WHEN data_quality_score >= 50 THEN 'medium'
                    ELSE 'low'
                END as tier,
                COUNT(*) as count
            FROM source_urls
            WHERE has_community_data = 1
            GROUP BY tier
        """)
        quality_distribution = {row[0]: row[1] for row in cursor.fetchall()}
        
    except sqlite3.OperationalError:
        # Tables don't exist yet
        total_urls = printed_urls = failed_urls = urls_with_data = 0
        avg_quality = 0
        high_quality_count = 0
        quality_distribution = {'high': 0, 'medium': 0, 'low': 0}
    
    # Pipeline status counts (new schema)
    try:
        cursor.execute(
            "SELECT status, COUNT(*) FROM community_pipeline_status GROUP BY status"
        )
        pipeline_statuses = {row[0]: row[1] for row in cursor.fetchall()}
    except sqlite3.OperationalError:
        pipeline_statuses = {}

    try:
        cursor.execute("SELECT COUNT(*) FROM community_urls")
        community_url_links = cursor.fetchone()[0]
    except sqlite3.OperationalError:
        community_url_links = 0

    # Recent communities
    cursor.execute("""
        SELECT name, slug, city, county_fips, created_at
        FROM communities
        ORDER BY created_at DESC
        LIMIT 10
    """)
    recent_communities = cursor.fetchall()
    
    conn.close()
    
    return templates.TemplateResponse(
        request,
        name="dashboard.html",
        context={
            "total_communities": total_communities,
            "gated_communities": gated_communities,
            "total_urls": total_urls,
            "printed_urls": printed_urls,
            "failed_urls": failed_urls,
            "urls_with_data": urls_with_data,
            "avg_quality": round(avg_quality, 1),
            "high_quality_count": high_quality_count,
            "quality_distribution": quality_distribution,
            "recent_communities": recent_communities,
            "pipeline_statuses": pipeline_statuses,
            "community_url_links": community_url_links,
        }
    )


@app.get("/communities", response_class=HTMLResponse)
async def communities_list(
    request: Request,
    search: Optional[str] = None,
    county: Optional[str] = None,
    status: Optional[str] = None,
):
    """List all communities with optional filtering (incl. pipeline status)."""
    conn = get_db()
    cursor = conn.cursor()

    has_pipeline = True
    try:
        cursor.execute("SELECT 1 FROM community_pipeline_status LIMIT 1")
    except sqlite3.OperationalError:
        has_pipeline = False

    pipeline_select = "ps.status as pipeline_status" if has_pipeline else "NULL as pipeline_status"
    pipeline_join = (
        "LEFT JOIN community_pipeline_status ps ON c.slug = ps.community_slug"
        if has_pipeline
        else ""
    )

    query = f"""
        SELECT 
            c.slug, c.name, c.city, c.county_fips, c.is_gated,
            c.data_source, c.created_at,
            COUNT(DISTINCT f.id) as fee_count,
            COUNT(DISTINCT a.id) as amenity_count,
            d.median_age, d.population,
            {pipeline_select}
        FROM communities c
        LEFT JOIN community_fees f ON c.slug = f.community_slug
        LEFT JOIN community_amenities a ON c.slug = a.community_slug
        LEFT JOIN community_demographics d ON c.slug = d.community_slug
        {pipeline_join}
    """
    
    params = []
    conditions = []
    
    if search:
        conditions.append("(c.name LIKE ? OR c.city LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    
    if county:
        conditions.append("c.county_fips = ?")
        params.append(county)

    if status and has_pipeline:
        conditions.append("ps.status = ?")
        params.append(status)
    
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    
    query += " GROUP BY c.slug ORDER BY c.name"
    
    cursor.execute(query, params)
    communities = cursor.fetchall()
    
    # Get unique counties for filter
    cursor.execute("SELECT DISTINCT county_fips FROM communities WHERE county_fips IS NOT NULL ORDER BY county_fips")
    counties = [row[0] for row in cursor.fetchall()]

    pipeline_statuses = []
    if has_pipeline:
        cursor.execute(
            "SELECT DISTINCT status FROM community_pipeline_status ORDER BY status"
        )
        pipeline_statuses = [row[0] for row in cursor.fetchall()]
    
    conn.close()
    
    return templates.TemplateResponse(
        request,
        name="communities.html",
        context={
            "communities": communities,
            "counties": counties,
            "search": search,
            "county": county,
            "status": status,
            "pipeline_statuses": pipeline_statuses,
        }
    )


@app.get("/communities/{slug}", response_class=HTMLResponse)
async def community_detail(request: Request, slug: str):
    """Detailed view of a single community."""
    conn = get_db()
    cursor = conn.cursor()
    
    # Community info
    cursor.execute("""
        SELECT * FROM communities WHERE slug = ?
    """, (slug,))
    community = cursor.fetchone()
    
    if not community:
        conn.close()
        return HTMLResponse("<h1>Community not found</h1>", status_code=404)
    
    # Fees
    cursor.execute("""
        SELECT * FROM community_fees
        WHERE community_slug = ?
        ORDER BY fee_type
    """, (slug,))
    fees = cursor.fetchall()
    
    # Amenities
    cursor.execute("""
        SELECT * FROM community_amenities
        WHERE community_slug = ?
        ORDER BY amenity
    """, (slug,))
    amenities = cursor.fetchall()
    
    # Demographics
    cursor.execute("""
        SELECT * FROM community_demographics
        WHERE community_slug = ?
    """, (slug,))
    demographics = cursor.fetchall()
    
    # Proximity
    cursor.execute("""
        SELECT * FROM proximity_metrics
        WHERE community_slug = ?
        ORDER BY category
    """, (slug,))
    proximity = cursor.fetchall()
    
    # Pipeline status (new schema)
    try:
        cursor.execute(
            """
            SELECT status, attempts, started_at, completed_at, last_error
            FROM community_pipeline_status
            WHERE community_slug = ?
            """,
            (slug,),
        )
        pipeline_status = cursor.fetchone()
    except sqlite3.OperationalError:
        pipeline_status = None

    # Per-condenser runs (new schema)
    try:
        cursor.execute(
            """
            SELECT condenser, status, elapsed, fees, amenities, proximity,
                   demographics_present, sources_consulted, errors, finished_at
            FROM community_condenser_runs
            WHERE community_slug = ?
            ORDER BY condenser
            """,
            (slug,),
        )
        condenser_runs = cursor.fetchall()
    except sqlite3.OperationalError:
        condenser_runs = []

    # Source URLs for this community via the community_urls join table,
    # falling back to the legacy source_urls.community_slug column.
    source_urls = []
    try:
        cursor.execute("""
            SELECT su.url, su.status, su.data_quality_score, su.data_types_found,
                   su.has_community_data, su.discovered_at,
                   cu.discovered_by, cu.inspected
            FROM community_urls cu
            JOIN source_urls su ON cu.url_id = su.id
            WHERE cu.community_slug = ?
            ORDER BY su.data_quality_score DESC
        """, (slug,))
        source_urls = cursor.fetchall()
    except sqlite3.OperationalError:
        source_urls = []

    if not source_urls:
        try:
            cursor.execute("""
                SELECT url, status, data_quality_score, data_types_found,
                       has_community_data, discovered_at,
                       discovered_by, NULL as inspected
                FROM source_urls
                WHERE community_slug = ?
                ORDER BY data_quality_score DESC
            """, (slug,))
            source_urls = cursor.fetchall()
        except sqlite3.OperationalError:
            source_urls = []
    
    conn.close()
    
    return templates.TemplateResponse(
        request,
        name="community_detail.html",
        context={
            "community": community,
            "fees": fees,
            "amenities": amenities,
            "demographics": demographics,
            "proximity": proximity,
            "source_urls": source_urls,
            "pipeline_status": pipeline_status,
            "condenser_runs": condenser_runs,
        }
    )


@app.get("/urls", response_class=HTMLResponse)
async def urls_list(
    request: Request,
    status: Optional[str] = None,
    min_quality: Optional[float] = None,
    has_data: Optional[bool] = None,
):
    """List all source URLs with quality metrics."""
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        try:
            cursor.execute("SELECT 1 FROM community_urls LIMIT 1")
            links_select = (
                "(SELECT COUNT(*) FROM community_urls cu "
                "WHERE cu.url_id = su.id) as community_count"
            )
        except sqlite3.OperationalError:
            links_select = "0 as community_count"

        query = f"""
            SELECT 
                su.url, su.domain, su.status, su.title, 
                su.data_quality_score, su.has_community_data, 
                su.data_types_found, su.data_summary,
                su.robot_friendly, su.captcha_detected, su.captcha_solved,
                su.discovered_at, su.accessed_at,
                {links_select}
            FROM source_urls su
        """
        
        params = []
        conditions = []
        
        if status:
            conditions.append("su.status = ?")
            params.append(status)
        
        if min_quality is not None:
            conditions.append("su.data_quality_score >= ?")
            params.append(min_quality)
        
        if has_data is not None:
            conditions.append("su.has_community_data = ?")
            params.append(1 if has_data else 0)
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY su.data_quality_score DESC, su.discovered_at DESC"
        
        cursor.execute(query, params)
        urls = cursor.fetchall()
        
    except sqlite3.OperationalError:
        urls = []
    
    conn.close()
    
    return templates.TemplateResponse(
        request,
        name="urls.html",
        context={
            "urls": urls,
            "status": status,
            "min_quality": min_quality,
            "has_data": has_data,
        }
    )


@app.get("/review-queue", response_class=HTMLResponse)
async def review_queue(request: Request):
    """Show URLs that need review (4xx failures)."""
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT 
                url, domain, status, failure_category, failure_detail,
                http_status, is_retryable, reviewed, review_note,
                discovered_at
            FROM source_urls
            WHERE status = 'failed_4xx' AND reviewed = 0
            ORDER BY discovered_at DESC
        """)
        review_items = cursor.fetchall()
    except sqlite3.OperationalError:
        review_items = []
    
    conn.close()
    
    return templates.TemplateResponse(
        request,
        name="review_queue.html",
        context={
            "review_items": review_items,
        }
    )


@app.get("/high-quality", response_class=HTMLResponse)
async def high_quality_urls(request: Request, min_score: float = 75.0):
    """Show URLs with high data quality scores."""
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT 
                url, domain, data_quality_score, data_types_found,
                data_summary, community_slug, discovered_at
            FROM source_urls
            WHERE data_quality_score >= ?
            ORDER BY data_quality_score DESC
        """, (min_score,))
        urls = cursor.fetchall()
    except sqlite3.OperationalError:
        urls = []
    
    conn.close()
    
    return templates.TemplateResponse(
        request,
        name="high_quality.html",
        context={
            "urls": urls,
            "min_score": min_score,
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
