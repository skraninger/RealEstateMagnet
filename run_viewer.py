#!/usr/bin/env python
"""
Run the RealEstateMagnet web viewer.

This script starts a local web server to view the database contents.
Open http://localhost:8000 in your browser after running.
"""

import sys
import uvicorn

def main():
    """Run the web viewer with optional port argument."""
    port = 8000
    
    # Parse command line arguments
    if len(sys.argv) > 1:
        if sys.argv[1] == "--port" and len(sys.argv) > 2:
            port = int(sys.argv[2])
        elif sys.argv[1].isdigit():
            port = int(sys.argv[1])
    
    print("Starting RealEstateMagnet Web Viewer...")
    print(f"Open http://localhost:{port} in your browser")
    print("Press Ctrl+C to stop the server")
    uvicorn.run("modules.web.viewer:app", host="127.0.0.1", port=port, reload=True)

if __name__ == "__main__":
    main()
