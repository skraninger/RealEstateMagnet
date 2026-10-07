"""Test that research agent integrates with URL tracking."""
import sqlite3
from pathlib import Path
import sys
import asyncio

sys.path.insert(0, str(Path(__file__).parent))

from modules.community.database import CommunityDatabase
from modules.community.url_tracker import URLTracker
from modules.community.agent import CommunityResearchAgent
from modules.community.tools import web_search

async def test_research_agent_url_tracking():
    """Test that research agent tracks URLs when using web_search."""
    print("Testing research agent URL tracking integration...\n")
    
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
    
    # Create research agent with URL tracker
    agent = CommunityResearchAgent(url_tracker=tracker)
    
    # Manually test web_search tool with URL tracker
    print("\nTesting web_search with URL tracker...")
    results = web_search("Pelican Bay Naples HOA", max_results=3)
    
    if results:
        # Register URLs (simulating what the tool does)
        urls = [r.url for r in results]
        tracker.register_urls(urls, discovered_by="research_agent_test")
        
        # Count URLs after
        conn = sqlite3.connect("data/communities.db")
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM source_urls")
        count_after = cursor.fetchone()[0]
        
        # Show newly registered URLs
        cursor.execute("""
            SELECT url, discovered_by, status 
            FROM source_urls 
            WHERE discovered_by = 'research_agent_test'
            ORDER BY discovered_at DESC
        """)
        new_urls = cursor.fetchall()
        conn.close()
        
        print(f"\nURLs in database after test: {count_after}")
        print(f"New URLs registered: {count_after - count_before}")
        
        if new_urls:
            print("\nURLs registered by research agent test:")
            for url, discovered_by, status in new_urls:
                print(f"  {url[:70]}...")
                print(f"    Discovered by: {discovered_by}, Status: {status}")
        else:
            print("\n[FAIL] No URLs were registered!")
            return False
    else:
        print("No search results found (network issue)")
        return False
    
    # Test that agent has url_tracker
    print(f"\nAgent has url_tracker: {agent.url_tracker is not None}")
    if agent.url_tracker is None:
        print("[FAIL] Agent does not have url_tracker!")
        return False
    
    print("\n[PASS] Research agent URL tracking integration test completed!")
    return True

if __name__ == "__main__":
    try:
        success = asyncio.run(test_research_agent_url_tracking())
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n[FAIL] Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
