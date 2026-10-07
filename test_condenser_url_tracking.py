"""Test URL tracking with actual condenser execution."""
import asyncio
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

from modules.community.database import CommunityDatabase
from modules.community.url_tracker import URLTracker
from modules.community.web_condenser import WebAICondenser
from modules.community.store import CommunityStore

async def test_condenser_url_tracking():
    """Test that condensers actually register URLs."""
    print("Testing URL tracking with WebAICondenser...\n")
    
    # Initialize components
    db = CommunityDatabase("data/communities.db")
    tracker = URLTracker(db)
    store = CommunityStore()
    
    # Count URLs before
    conn = sqlite3.connect("data/communities.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM source_urls")
    count_before = cursor.fetchone()[0]
    conn.close()
    
    print(f"URLs in database before test: {count_before}")
    
    # Create condenser with URL tracker
    condenser = WebAICondenser(
        store=store,
        database=db,
        url_tracker=tracker
    )
    
    print("\nRunning enrich_from_web for 'Pelican Bay'...")
    print("(This should register discovered URLs)\n")
    
    # Run the condenser (limit to avoid too many requests)
    try:
        result = await condenser.enrich_from_web("Pelican Bay", city="Naples")
        
        # Count URLs after
        conn = sqlite3.connect("data/communities.db")
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM source_urls")
        count_after = cursor.fetchone()[0]
        
        cursor.execute("SELECT url, status, discovered_by FROM source_urls WHERE discovered_by = 'web_condenser'")
        new_urls = cursor.fetchall()
        conn.close()
        
        print(f"\nURLs in database after test: {count_after}")
        print(f"New URLs registered: {count_after - count_before}")
        
        if new_urls:
            print("\nSample URLs registered by web_condenser:")
            for url, status, discovered_by in new_urls[:5]:
                print(f"  {url[:60]}...")
                print(f"    Status: {status}, Discovered by: {discovered_by}")
        else:
            print("\nNo URLs were registered!")
            print("This indicates the URL tracker integration is not working.")
            
    except Exception as e:
        print(f"\nError during test: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_condenser_url_tracking())
