Run the Phase 1 Source Discovery Engine against all registered Florida structured data sources (Socrata, ArcGIS, CKAN, direct).

Steps:
1. Run: `python3 -m modules.discovery.source_discovery --output data/catalogs/florida_sources.json --log-level INFO`
2. Report how many endpoints were discovered, broken down by protocol type (socrata/arcgis/ckan/direct).
3. Highlight any sources that returned errors or were unreachable.
4. Show the top 5 entries by dataset size or relevance.

If the user passes `--web` or mentions "include unstructured", add `--include-unstructured` to the command (this is much slower — warn the user it may take 20–30 minutes).

If the user specifies categories (e.g. "just property and tax"), add `--categories property tax` to the command.

After the run, summarise the output catalog path and suggest next steps (e.g. running `/schema` on high-value endpoints).
