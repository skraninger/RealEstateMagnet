# RealEstateMagnet Web Viewer

A local web application for viewing and exploring the RealEstateMagnet database.

## Features

- **Dashboard**: Overview statistics including community counts, URL tracking metrics, and data quality distribution
- **Communities**: Browse all communities with filtering by name and county, view detailed information including fees, amenities, demographics, and proximity data
- **Source URLs**: View all tracked URLs with data quality scores, robot-friendly status, and CAPTCHA detection results
- **High Quality URLs**: Find URLs with the highest data quality scores (configurable threshold)
- **Review Queue**: Identify URLs that failed with 4xx errors and need manual review
- **Inline editing**: Edit and delete fact rows, pipeline status, condenser runs, and source URLs directly from the page (see [Editing and deleting rows](#editing-and-deleting-rows))

## Running the Viewer

### Using PowerShell (Windows)

```powershell
# Start with default port 8000
.\scripts\run-web-viewer.ps1

# Start on custom port
.\scripts\run-web-viewer.ps1 -Port 8080

# Start without opening browser
.\scripts\run-web-viewer.ps1 -NoBrowser
```

### Using Python directly

```bash
# Start with default port 8000
python run_viewer.py

# Start on custom port
python run_viewer.py --port 8080

# Or
python run_viewer.py 8080
```

### Using FastAPI directly

```bash
# Start with uvicorn
uvicorn modules.web.viewer:app --host 127.0.0.1 --port 8000 --reload
```

Then open your browser to http://localhost:8000

## Views

### Dashboard (`/`)

The main landing page shows:
- Total communities and gated communities count
- URL tracking statistics (total, printed, failed)
- URLs with community data and average quality score
- Data quality distribution (high/medium/low tiers)
- Recent communities added to the database
- Review queue alert if there are failed URLs

### Communities (`/communities`)

Browse all communities with:
- Search by name or city
- Filter by county
- Filter by pipeline status
- Summary columns: gated status, fees count, amenities count, median age, population

#### Drilling down into a community

The list is the entry point for the per-community view. To descend from a
community to its associated facts, click the community **name** — the name is a
link to `/communities/<slug>`. You can also navigate directly:

```
http://localhost:8000/communities/<slug>
```

For example, `http://localhost:8000/communities/gables-estates`.

The same detail page is linked from the **Dashboard → Recent Communities**
table, so a community can be reached from either place.

### Community Detail (`/communities/{slug}`)

The drill-down page for one community. It is served by
`community_detail()` in `modules/web/viewer.py` and rendered by
`templates/community_detail.html`. Every section links back to its source
table so the UI matches the database exactly:

| Section | Source table | What it shows |
|---------|--------------|---------------|
| Basic info | `communities` | Slug, city, county, gated status, HOA name, CDD name, data source, overview/notes, created/updated |
| Pipeline Status | `community_pipeline_status` | Status badge, attempts, start/completed timestamps, last error |
| Condenser Runs | `community_condenser_runs` | One row per condenser: status, elapsed, fees/amenities/proximity counts, demographics present, sources consulted |
| Fees | `community_fees` | Fee type, amount, period, currency, source URL, confidence |
| Amenities | `community_amenities` | Amenity, detail, source URL, confidence |
| Demographics | `community_demographics` | Median age, median income, owner occupancy %, population, data year, source |
| Proximity | `proximity_metrics` | Category, nearest facility, distance (miles), source |
| Source URLs | `community_urls` → `source_urls` | URLs attributed to the community (falls back to the legacy `source_urls.community_slug` column) with status, quality score, data types, discovery info |

Empty sections are simply omitted, so a community with no fees yet shows no
Fees card. If the slug is unknown the route returns HTTP 404.

Every fact row (Fees, Amenities, Demographics, Proximity), the Pipeline Status
row, and each Condenser Run row has **Edit** and **Delete** actions — see
[Editing and deleting rows](#editing-and-deleting-rows).

### Source URLs (`/urls`)

View all tracked URLs with:
- Filter by status (printed, failed_4xx, failed_5xx, pending)
- Filter by minimum quality score
- Filter by whether the URL contains community data
- View quality scores, data types found, robot-friendly status, CAPTCHA results
- Edit a URL's metadata (title, status, quality, has-data, reviewed, review note,
  failure info, etc.) or delete it — see
  [Editing and deleting rows](#editing-and-deleting-rows)

### Editing and deleting rows

The viewer can modify the database, not just read it. Editing is **inline**: the
row is replaced by a short form and a `Save`/`Cancel` pair.

| Page | What you can edit / delete |
|------|----------------------------|
| `/communities/{slug}` | Fee, amenity, demographics, and proximity rows (all fields in `FACT_TABLES`) |
| `/communities/{slug}` | The `community_pipeline_status` row (status, attempts, sort order, last error) — edit or delete |
| `/communities/{slug}` | Each `community_condenser_runs` row (status, counts, sources, errors) — edit or delete |
| `/urls` | `source_urls` metadata (title, domain, status, quality, has-data, reviewed, review note, failure category/detail, retryable, robot-friendly, community slug, HTTP status, data summary) — edit or delete. The `url` itself is read-only (it is the UNIQUE key); deleting also removes its `community_urls` links. |

Implementation notes:

- **Edit mode** is server-rendered from the query string
  `?edit_table=<key>&edit_id=<row_id>` — no JavaScript required. The Edit link
  sets these; Cancel/save returns to the plain page.
- Controls are rendered by the shared macro
  `templates/_macros.html::actions`. A row's inputs connect to an empty
  `<form id="{table}-edit-{row_id}">` via the HTML5 `form="..."` attribute so a
  `<form>` never has to sit inside a `<tr>`.
- Writes accept only **whitelisted columns** defined in `modules/web/viewer.py`
  (`FACT_TABLES`, `PIPELINE_FIELDS`, `CONDENSER_RUN_FIELDS`, `URL_FIELDS`); values
  are always bound parameters. Enum fields (pipeline/condenser statuses) ignore
  invalid values. A blank *required* field (e.g. fee type) is left unchanged
  rather than nulled.
- After a write the server responds **303 See Other**, redirecting back to the
  page so refreshing does not resubmit.
- There is **no authentication** — the viewer binds to `127.0.0.1` and is
  intended for local use. Do not expose it to an untrusted network.
- Endpoints: `POST /communities/{slug}/facts/{table}/{id}/edit|delete`,
  `POST /communities/{slug}/pipeline/edit|delete`,
  `POST /communities/{slug}/condenser-runs/{id}/edit|delete`,
  `POST /urls/{id}/edit|delete`.

### High Quality URLs (`/high-quality`)

Find the best data sources:
- Configure minimum quality score threshold (default: 75)
- View URLs sorted by quality score
- See data types found and data summaries
- Link to associated communities

### Review Queue (`/review-queue`)

Identify problematic URLs:
- Shows all URLs with 4xx failures that haven't been reviewed
- Displays HTTP status codes, failure categories, and failure details
- Indicates whether failures are retryable
- Helps prioritize manual review efforts

## Database Requirements

The viewer reads from the SQLite database at `data/communities.db`. The database must exist and contain the following tables:

- `communities` - Community records
- `community_fees` - Fee information
- `community_amenities` - Amenity information
- `community_demographics` - Demographic data
- `proximity_metrics` - Proximity to facilities
- `source_urls` - URL tracking with quality metrics
- `community_urls` - Links source URLs to the communities they were inspected for
- `community_pipeline_status` - Per-community pipeline progress
- `community_condenser_runs` - Per-community condenser step results
- `schema_migrations` - Applied-migration markers

If optional tables (e.g. `source_urls`, `community_urls`, `community_pipeline_status`, `community_condenser_runs`) are missing, the viewer degrades gracefully: it hides the corresponding cards and shows dashes for URL-related statistics instead of erroring.

## Technology Stack

- **FastAPI** - Web framework
- **Jinja2** - HTML templating
- **SQLite** - Database
- **Uvicorn** - ASGI server

All dependencies are already in `requirements.txt`:
- `fastapi>=0.111.0`
- `uvicorn[standard]>=0.29.0`
- `jinja2>=3.1.0`
- `python-multipart>=0.0.9` (form parsing for the edit/delete actions)

## Screenshots

The web viewer provides a clean, modern interface with:
- Responsive design that works on desktop and mobile
- Color-coded quality scores (green for high, orange for medium, red for low)
- Badge indicators for status and boolean values
- Sortable tables with filtering capabilities
- Navigation bar for easy access to all views

## Troubleshooting

### Port already in use
If port 8000 is already in use, specify a different port:
```powershell
.\scripts\run-web-viewer.ps1 -Port 8080
```

### Database not found
Make sure the database exists at `data/communities.db`. If it doesn't exist, run the pipeline first to generate it.

### Module not found
Make sure you're running from the project root directory and that all dependencies are installed:
```bash
pip install -r requirements.txt
```

## Future Enhancements

Potential improvements:
- Export data to CSV/JSON
- Interactive charts for data quality trends
- Map view for community locations
- Bulk edit/delete of multiple selected rows
- Adding brand-new fact rows (currently rows are edit/delete only)
- Real-time updates when pipeline is running
- Advanced search with full-text search
- API endpoints for programmatic access
