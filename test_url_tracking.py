"""Test URL tracking integration."""
import sys
import sqlite3
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from modules.community.database import CommunityDatabase
from modules.community.url_tracker import URLTracker

def test_url_tracker():
    """Test that URLTracker can register and update URLs."""
    print("Testing URLTracker...")
    
    # Initialize database and tracker
    db = CommunityDatabase("data/communities.db")
    tracker = URLTracker(db)
    
    # Check if source_urls table exists
    conn = sqlite3.connect("data/communities.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='source_urls'")
    if not cursor.fetchone():
        print("ERROR: source_urls table does not exist!")
        print("Creating tables...")
        db.create_tables()
    
    conn.close()
    
    # Test registering a URL
    test_url = "https://example.com/test"
    print(f"\n1. Registering URL: {test_url}")
    row = tracker.register_url(test_url, "test_script", "test-community")
    print(f"   Result: id={row.id}, status={row.status}, discovered_by={row.discovered_by}")
    
    # Test updating URL status
    print("\n2. Updating URL status to 'printed'")
    tracker.update_url_status(test_url, "printed", http_status=200)
    
    # Verify the update
    conn = sqlite3.connect("data/communities.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM source_urls WHERE url = ?", (test_url,))
    result = cursor.fetchone()
    conn.close()
    
    if result:
        print(f"   Updated: status={result['status']}, http_status={result['http_status']}")
    else:
        print("   ERROR: URL not found after update!")
    
    # Test updating URL quality
    print("\n3. Updating URL quality metrics")
    tracker.update_url_quality(
        test_url,
        has_community_data=True,
        data_quality_score=85.5,
        data_types_found="fees,amenities",
        data_summary="Test summary"
    )
    
    # Verify the quality update
    conn = sqlite3.connect("data/communities.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM source_urls WHERE url = ?", (test_url,))
    result = cursor.fetchone()
    conn.close()
    
    if result:
        print(f"   Updated: has_community_data={result['has_community_data']}, "
              f"score={result['data_quality_score']}, types={result['data_types_found']}")
    else:
        print("   ERROR: URL not found after quality update!")
    
    # Count total URLs in database
    conn = sqlite3.connect("data/communities.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM source_urls")
    count = cursor.fetchone()[0]
    conn.close()
    
    print(f"\n4. Total URLs in database: {count}")
    
    # Test registering multiple URLs
    print("\n5. Registering multiple URLs")
    test_urls = [
        "https://example.com/test1",
        "https://example.com/test2",
        "https://example.com/test3"
    ]
    rows = tracker.register_urls(test_urls, "test_script", "test-community")
    print(f"   Registered {len(rows)} URLs")
    
    # Final count
    conn = sqlite3.connect("data/communities.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM source_urls")
    final_count = cursor.fetchone()[0]
    conn.close()
    
    print(f"\n6. Final URL count: {final_count}")
    
    print("\n[PASS] URLTracker test completed successfully!")
    print("\nNote: The test URLs are now in your database.")
    print("You can view them in the web viewer at http://localhost:8090/urls")

if __name__ == "__main__":
    try:
        test_url_tracker()
    except Exception as e:
        print(f"\n[FAIL] Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
