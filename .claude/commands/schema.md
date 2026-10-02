Run the Phase 1 Schema Analyzer against a data endpoint to map its fields to the Master Florida Schema.

The user must provide a URL (JSON, CSV, GeoJSON, or HTML page with a table). If they didn't include one, ask for it.

Steps:
1. Import and run SchemaAnalyzer:
   ```python
   import asyncio
   from modules.discovery.schema_analyzer import SchemaAnalyzer
   analyzer = SchemaAnalyzer()
   report = asyncio.run(analyzer.analyze("<URL>"))
   print(report)
   ```
2. Present the results clearly:
   - Detected format and record count
   - Master Schema Coverage table: which of {uuid, geo_point, source_url, standard_score, last_updated} were found and via which raw field name
   - Fields with HIGH confidence mapping (≥ 0.8) — list raw_name → canonical_name
   - Unmapped fields — list fields that have no canonical mapping
   - Notes (e.g. "separate lat/lon found", "GeoJSON geometry detected")
3. If geo_point is missing but lat/lon fields are present, note that the transformation pipeline will need to combine them.
4. If fewer than 3 master schema fields are covered, flag this as a LOW COVERAGE source.
5. Suggest what transformation logic will be needed to normalize this source to the master schema.

Master Florida Schema fields: uuid, geo_point, source_url, standard_score, last_updated.
