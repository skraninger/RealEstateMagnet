Check the development environment and report whether all required components are correctly installed and configured.

Run these checks and report each as ✅ / ⚠️ / ❌:

```bash
# Python version (need 3.12+)
python3 --version

# Core packages
python3 -c "import httpx, bs4, playwright, pydantic, fastapi, sqlalchemy, geoalchemy2, tenacity; print('All packages OK')"

# Playwright browser
python3 -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); b = p.chromium.launch(); b.close(); p.stop(); print('Playwright Chromium OK')"

# PostgreSQL client
psql --version 2>/dev/null || echo "psql not found (OK if running outside container)"

# Environment variables
python3 -c "
import os
required = ['DATABASE_URL']
optional = ['SECRET_KEY', 'CRAWL_DELAY_SECONDS', 'MAPBOX_PUBLIC_TOKEN', 'CENSUS_API_KEY']
for k in required:
    v = os.getenv(k, '')
    print(f'  {k}: {\"SET\" if v else \"MISSING\"}')
for k in optional:
    v = os.getenv(k, '')
    print(f'  {k}: {\"SET\" if v else \"not set (optional)\"}')
"

# Project structure
python3 -c "
from pathlib import Path
checks = [
    'modules/discovery/__init__.py',
    'modules/discovery/florida_sources.py',
    'modules/discovery/source_discovery.py',
    'modules/discovery/web_crawler.py',
    'modules/discovery/schema_analyzer.py',
    '.devcontainer/docker-compose.yml',
    '.env',
    'requirements.txt',
]
for f in checks:
    exists = Path(f).exists()
    print(f'  {\"OK\" if exists else \"MISSING\"}: {f}')
"
```

After running all checks, summarise:
- What is working
- What is missing or broken, with fix commands
- Whether the devcontainer is active (check for `/.dockerenv` file)
- Recommend next action (e.g. "Run /discover to start Phase 1" or "Run /db-check to verify PostGIS")
