"""
Quick test to verify vision capabilities are working.
This script takes a screenshot of a simple webpage and analyzes it with the multimodal LLM.
"""
import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.community.tools import take_screenshot, analyze_screenshot


async def test_vision():
    """Test vision capabilities with a simple webpage."""
    print("Testing vision capabilities...")
    print("=" * 60)
    
    # Use a simple, stable webpage for testing
    test_url = "https://example.com"
    
    print(f"\n1. Taking screenshot of {test_url}...")
    screenshot_path = await take_screenshot(test_url)
    
    if not screenshot_path:
        print("ERROR: Failed to take screenshot")
        return False
    
    print(f"   Screenshot saved to: {screenshot_path}")
    
    # Check file size
    file_size = Path(screenshot_path).stat().st_size
    print(f"   File size: {file_size / 1024:.1f} KB")
    
    print("\n2. Analyzing screenshot with multimodal LLM...")
    print("   (This may take 30-60 seconds on first run)")
    
    analysis = await analyze_screenshot(
        screenshot_path,
        "What is shown on this webpage? Describe the main content and any text visible."
    )
    
    if not analysis:
        print("ERROR: Failed to analyze screenshot")
        return False
    
    print("\n3. Analysis result:")
    print("-" * 60)
    print(analysis)
    print("-" * 60)
    
    # Cleanup
    Path(screenshot_path).unlink()
    print(f"\n4. Cleaned up screenshot file")
    
    print("\n" + "=" * 60)
    print("SUCCESS: Vision capabilities are working!")
    print("=" * 60)
    return True


if __name__ == "__main__":
    success = asyncio.run(test_vision())
    sys.exit(0 if success else 1)
