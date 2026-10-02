#!/usr/bin/env bash
# =============================================================================
# RealEstateMagnet — PostgreSQL / PostGIS Initialisation Script
# =============================================================================
# This script is mounted into the PostGIS container at:
#   /docker-entrypoint-initdb.d/01-init.sh
#
# It runs automatically on first container start (when the data volume is empty).
# It is idempotent — safe to re-run manually via:
#   docker exec -it <container> bash /docker-entrypoint-initdb.d/01-init.sh
#
# What it does:
#   1. Enable PostGIS, topology, and UUID extensions
#   2. Create 'raw' and 'staging' schemas
#   3. Set search_path and grant permissions to the app user
# =============================================================================

set -euo pipefail

DB_NAME="${POSTGRES_DB:-realestate_magnet}"
DB_USER="${POSTGRES_USER:-rem}"

echo "==> Initialising database: $DB_NAME"

# Run all DDL as the superuser (postgres) inside the target database
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$DB_NAME" <<-EOSQL

    -- ── Extensions ────────────────────────────────────────────────────────
    CREATE EXTENSION IF NOT EXISTS postgis;
    CREATE EXTENSION IF NOT EXISTS postgis_topology;
    CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
    CREATE EXTENSION IF NOT EXISTS btree_gin;      -- GIN index on standard types
    CREATE EXTENSION IF NOT EXISTS pg_trgm;        -- trigram similarity (fuzzy search)

    -- ── Schemas ───────────────────────────────────────────────────────────
    -- raw     : ingested data exactly as received, before normalisation
    -- staging : normalisation pipeline working area
    -- public  : final, normalised master records (default schema)

    CREATE SCHEMA IF NOT EXISTS raw;
    CREATE SCHEMA IF NOT EXISTS staging;
    -- public already exists

    -- ── Search path ───────────────────────────────────────────────────────
    ALTER DATABASE "$DB_NAME" SET search_path TO public, staging, raw;

    -- ── App user grants ───────────────────────────────────────────────────
    -- Grant usage on all three schemas
    GRANT USAGE  ON SCHEMA public,  staging, raw TO "$DB_USER";
    GRANT CREATE ON SCHEMA public,  staging, raw TO "$DB_USER";

    -- Future tables created by other users are also accessible
    ALTER DEFAULT PRIVILEGES IN SCHEMA public  GRANT ALL ON TABLES    TO "$DB_USER";
    ALTER DEFAULT PRIVILEGES IN SCHEMA staging GRANT ALL ON TABLES    TO "$DB_USER";
    ALTER DEFAULT PRIVILEGES IN SCHEMA raw     GRANT ALL ON TABLES    TO "$DB_USER";
    ALTER DEFAULT PRIVILEGES IN SCHEMA public  GRANT ALL ON SEQUENCES TO "$DB_USER";
    ALTER DEFAULT PRIVILEGES IN SCHEMA staging GRANT ALL ON SEQUENCES TO "$DB_USER";
    ALTER DEFAULT PRIVILEGES IN SCHEMA raw     GRANT ALL ON SEQUENCES TO "$DB_USER";

    -- ── Source catalog table ──────────────────────────────────────────────
    -- Tracks every discovered data source from Phase 1 discovery runs.
    CREATE TABLE IF NOT EXISTS raw.source_catalog (
        id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
        source_name     TEXT        NOT NULL,
        protocol        TEXT        NOT NULL,          -- socrata | arcgis | ckan | direct | web
        categories      TEXT[]      NOT NULL DEFAULT '{}',
        endpoint_url    TEXT        NOT NULL,
        format_hint     TEXT,
        signal_type     TEXT,                          -- download_link | html_table | api_pattern | …
        relevance_score FLOAT,
        requires_auth   BOOLEAN     NOT NULL DEFAULT FALSE,
        requires_js     BOOLEAN     NOT NULL DEFAULT FALSE,
        description     TEXT,
        discovered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_probed_at  TIMESTAMPTZ,
        is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
        raw_metadata    JSONB       NOT NULL DEFAULT '{}'::JSONB,
        UNIQUE (endpoint_url)
    );

    CREATE INDEX IF NOT EXISTS idx_source_catalog_protocol
        ON raw.source_catalog (protocol);
    CREATE INDEX IF NOT EXISTS idx_source_catalog_categories
        ON raw.source_catalog USING GIN (categories);

    -- ── Master property record table (placeholder, Phase 3) ──────────────
    -- Implements the Master Florida Schema from FSD §3.
    -- geo_point uses SRID 4326 (WGS84 / standard GPS coordinates).
    CREATE TABLE IF NOT EXISTS public.property_master (
        uuid            UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
        geo_point       GEOMETRY(Point, 4326),
        source_url      TEXT        NOT NULL,
        standard_score  FLOAT       CHECK (standard_score BETWEEN 0 AND 100),
        last_updated    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

        -- Property fields (populated by transformation pipeline)
        parcel_id       TEXT,
        address         TEXT,
        city            TEXT,
        county          TEXT,
        zip_code        TEXT,
        state           TEXT        NOT NULL DEFAULT 'FL',
        assessed_value  NUMERIC(14,2),
        taxable_value   NUMERIC(14,2),
        tax_amount      NUMERIC(12,2),
        tax_rate        FLOAT,
        year_built      SMALLINT,
        sqft            INTEGER,
        bedrooms        SMALLINT,
        bathrooms       FLOAT,

        -- Decision metrics
        flood_zone      TEXT,
        school_grade    TEXT,
        crime_rate      FLOAT,

        -- Provenance
        raw_source_id   UUID        REFERENCES raw.source_catalog(id),
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_property_geo
        ON public.property_master USING GIST (geo_point);
    CREATE INDEX IF NOT EXISTS idx_property_county
        ON public.property_master (county);
    CREATE INDEX IF NOT EXISTS idx_property_parcel
        ON public.property_master (parcel_id);
    CREATE INDEX IF NOT EXISTS idx_property_standard_score
        ON public.property_master (standard_score);

    -- ── Trigger: auto-update 'updated_at' ────────────────────────────────
    CREATE OR REPLACE FUNCTION public.set_updated_at()
    RETURNS TRIGGER LANGUAGE plpgsql AS \$\$
    BEGIN
        NEW.updated_at = NOW();
        RETURN NEW;
    END;
    \$\$;

    DROP TRIGGER IF EXISTS trg_property_updated_at ON public.property_master;
    CREATE TRIGGER trg_property_updated_at
        BEFORE UPDATE ON public.property_master
        FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

EOSQL

echo "==> Database initialisation complete."
echo "    Extensions : postgis, postgis_topology, uuid-ossp, btree_gin, pg_trgm"
echo "    Schemas    : public, staging, raw"
echo "    Tables     : raw.source_catalog, public.property_master"
