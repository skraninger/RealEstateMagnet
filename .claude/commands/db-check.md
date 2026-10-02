Verify that the PostgreSQL + PostGIS database is reachable and correctly configured.

Steps:
1. Check if the devcontainer is running: `docker ps 2>/dev/null | grep -E "db|postgis"` (if on host) or just try the connection directly.
2. Run a series of connectivity and version checks using psql or Python:

   ```bash
   python3 -c "
   import asyncio, asyncpg, os
   async def check():
       url = os.getenv('DATABASE_URL', 'postgresql://rem:rem_dev@localhost:5432/realestate_magnet')
       conn = await asyncpg.connect(url)
       pg_ver    = await conn.fetchval('SELECT version()')
       postgis   = await conn.fetchval('SELECT PostGIS_Full_Version()')
       schemas   = await conn.fetch(\"SELECT schema_name FROM information_schema.schemata WHERE schema_name IN ('public','staging','raw') ORDER BY 1\")
       tables    = await conn.fetch(\"SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN ('public','staging','raw') ORDER BY 1,2\")
       exts      = await conn.fetch(\"SELECT extname FROM pg_extension ORDER BY 1\")
       await conn.close()
       print('PostgreSQL:', pg_ver[:60])
       print('PostGIS   :', postgis[:80])
       print('Schemas   :', [r['schema_name'] for r in schemas])
       print('Tables    :', [(r['schemaname'],r['tablename']) for r in tables])
       print('Extensions:', [r['extname'] for r in exts])
   asyncio.run(check())
   "
   ```

3. Report:
   - ✅ / ❌ for each check: connection, PostGIS version, schemas (raw, staging, public), tables (source_catalog, property_master), required extensions
   - If anything is missing, suggest the fix (e.g. "Run docker compose up -d" or "Re-run init-database.sh")

4. If the database is NOT reachable, provide the relevant startup commands:
   ```bash
   # Inside devcontainer:
   docker compose -f .devcontainer/docker-compose.yml up db -d

   # Or from local host:
   docker compose -f .devcontainer/docker-compose.yml up -d
   ```
