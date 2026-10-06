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
        "dashboard.html",
        {
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
        }
    )


@app.get("/communities", response_class=HTMLResponse)
async def communities_list(
    request: Request,
    search: Optional[str] = None,
    county: Optional[str] = None,
):
    """List all communities with optional filtering."""
    conn = get_db()
    cursor = conn.cursor()
    
    query = """
        SELECT 
            c.slug, c.name, c.city, c.county_fips, c.is_gated,
            c.data_source, c.created_at,
            COUNT(DISTINCT f.id) as fee_count,
            COUNT(DISTINCT a.id) as amenity_count,
            d.median_age, d.population
        FROM communities c
        LEFT JOIN community_fees f ON c.slug = f.community_slug
        LEFT JOIN community_amenities a ON c.slug = a.community_slug
        LEFT JOIN community_demographics d ON c.slug = d.community_slug
    """
    
    params = []
    conditions = []
    
    if search:
        conditions.append("(c.name LIKE ? OR c.city LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    
    if county:
        conditions.append("c.county_fips = ?")
        params.append(county)
    
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    
    query += " GROUP BY c.slug ORDER BY c.name"
    
    cursor.execute(query, params)
    communities = cursor.fetchall()
    
    # Get unique counties for filter
    cursor.execute("SELECT DISTINCT county_fips FROM communities WHERE county_fips IS NOT NULL ORDER BY county_fips")
    counties = [row[0] for row in cursor.fetchall()]
    
    conn.close()
    
    return templates.TemplateResponse(
        request,
        "communities.html",
        {
            "communities": communities,
            "counties": counties,
            "search": search,
            "county": county,
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
    
    # Source URLs for this community
    try:
        cursor.execute("""
            SELECT url, status, data_quality_score, data_types_found, 
                   has_community_data, discovered_at
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
        "community_detail.html",
        {
            "community": community,
            "fees": fees,
            "amenities": amenities,
            "demographics": demographics,
            "proximity": proximity,
            "source_urls": source_urls,
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
        query = """
            SELECT 
                url, domain, status, title, 
                data_quality_score, has_community_data, 
                data_types_found, data_summary,
                robot_friendly, captcha_detected, captcha_solved,
                discovered_at, accessed_at
            FROM source_urls
        """
        
        params = []
        conditions = []
        
        if status:
            conditions.append("status = ?")
            params.append(status)
        
        if min_quality is not None:
            conditions.append("data_quality_score >= ?")
            params.append(min_quality)
        
        if has_data is not None:
            conditions.append("has_community_data = ?")
            params.append(1 if has_data else 0)
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY data_quality_score DESC, discovered_at DESC"
        
        cursor.execute(query, params)
        urls = cursor.fetchall()
        
    except sqlite3.OperationalError:
        urls = []
    
    conn.close()
    
    return templates.TemplateResponse(
        request,
        "urls.html",
        {
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
        "review_queue.html",
        {
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
        "high_quality.html",
        {
            "urls": urls,
            "min_score": min_score,
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
