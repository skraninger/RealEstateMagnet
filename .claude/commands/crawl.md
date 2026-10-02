Run the Phase 1 WebCrawler against a URL to discover unstructured data signals (download links, HTML tables, API patterns, search forms).

The user must provide a URL. If they didn't include one, ask for it.

Steps:
1. Determine crawl options from the user's message:
   - `--depth N` (default 2 for HOA/CDD sites, 1 for MLS/gated sites)
   - `--js` flag if the site is known to be JS-rendered (MLS portals, court clerk portals)
   - `--max-pages N` if the user wants to limit scope
2. Run: `python3 -m modules.discovery.web_crawler --url <URL> --depth <N> [--js] --output data/catalogs/<slug>.json --log-level INFO`
   where `<slug>` is a short name derived from the URL hostname.
3. Parse and summarise the CrawlResult:
   - Pages visited, signals found (by type)
   - Top 5 signals by relevance score with their URLs
   - Any gated URLs (401/403) — suggest auth_notes if available from florida_sources.py
   - Any robots.txt blocks
4. If download links were found (.csv, .xlsx, .geojson), offer to run `/schema` on the top one.

Known JS-required sites from the registry: MLS portals, Miami-Dade/Broward clerk of courts, The Villages, Babcock Ranch.
Known requires_auth sites: Stellar MLS, BeachesMLS — remind the user to provide auth cookies.
