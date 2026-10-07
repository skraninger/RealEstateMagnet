"""Test URL registration during web search (no model server needed)."""
import asyncio
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

from modules.community.database import CommunityDatabase
from modules.community.url_tracker import URLTracker
from modules.community.tools import web_search

async def test_url_registration():
    """Test that URLs are registered when discovered via web search."""
    print("Testing URL registration during web search...\n")
    
    # Initialize components
    db = CommunityDatabase("data/communities.db")
    tracker = URLTracker(db)
    
    # Count URLs before
    conn = sqlite3.connect("data/communities.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM source_urls")
    count_before = cursor.fetchone()[0]
    conn.close()
    
    print(f"URLs in database before test: {count_before}")
    
    # Perform a web search
    print("\nPerforming web search for 'Pelican Bay Naples HOA fees'...")
    search_results = web_search("Pelican Bay Naples HOA fees", max_results=5)
    
    print(f"Found {len(search_results)} search results")
    
    if not search_results:
        print("No search results found. This might be a network issue.")
        return
    
    # Register URLs (this is what the condenser should do)
    print("\nRegistering discovered URLs...")
    urls = [sr.url for sr in search_results]
    tracker.register_urls(urls, discovered_by="test_search")
    
    # Count URLs after
    conn = sqlite3.connect("data/communities.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM source_urls")
    count_after = cursor.fetchone()[0]
    
    # Show newly registered URLs
    cursor.execute("""
        SELECT url, status, discovered_by, discovered_at 
        FROM source_urls 
        WHERE discovered_by = 'test_search'
        ORDER BY discovered_at DESC
    """)
    new_urls = cursor.fetchall()
    conn.close()
    
    print(f"\nURLs in database after test: {count_after}")
    print(f"New URLs registered: {count_after - count_before}")
    
    if new_urls:
        print("\nURLs registered by this test:")
        for url, status, discovered_by, discovered_at in new_urls:
            print(f"  {url[:70]}...")
            print(f"    Status: {status}, Discovered by: {discovered_by}")
    else:
        print("\nNo URLs were registered!")
        print("This indicates the URL tracker integration is not working.")

if __name__ == "__main__":
    asyncio.run(test_url_registration())
