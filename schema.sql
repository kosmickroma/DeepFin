-- schema.sql
--
-- Create the two Redfin harvest tables in the target PostgreSQL/PostGIS database.
-- Run via: psql $DATABASE_URL -f schema.sql
-- Or:      python scripts/setup_db.py
--
-- These tables live in the same DB as lot-ledger's parcels/tad_parcels tables.
-- PostGIS must already be enabled (it is, on the Cloud SQL instance).

-- ============================================================
-- Active (for-sale) listings
-- ============================================================

CREATE TABLE IF NOT EXISTS redfin_active (
    id              BIGSERIAL PRIMARY KEY,

    -- Address + location
    address         TEXT        NOT NULL,
    addr_key        TEXT        NOT NULL,   -- normalized key for parcel matching
    city            TEXT,
    zip             TEXT,
    lat             DOUBLE PRECISION NOT NULL,
    lng             DOUBLE PRECISION NOT NULL,
    geom            GEOMETRY(Point, 4326),

    -- Listing details
    property_type   TEXT,
    price           INTEGER,
    beds            SMALLINT,
    baths           SMALLINT,
    sqft            INTEGER,
    lot_sqft        INTEGER,
    yr_built        SMALLINT,
    dom             SMALLINT,               -- days on market
    price_per_sqft  INTEGER,
    hoa_monthly     INTEGER,
    status          TEXT,                   -- Active, Pending, Coming Soon, etc.

    -- Source identifiers
    mls_num         TEXT,
    listing_url     TEXT        UNIQUE,     -- primary dedup key

    -- Housekeeping
    fetched_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_redfin_active_geom
    ON redfin_active USING GIST(geom);

CREATE INDEX IF NOT EXISTS idx_redfin_active_addr_key
    ON redfin_active(addr_key);

CREATE INDEX IF NOT EXISTS idx_redfin_active_status
    ON redfin_active(status);

CREATE INDEX IF NOT EXISTS idx_redfin_active_fetched_at
    ON redfin_active(fetched_at);

COMMENT ON TABLE redfin_active IS
    'Redfin for-sale listings harvested by the DeepFin crawler. '
    'Upserted on listing_url; fetched_at updated each crawl pass.';


-- ============================================================
-- Sold listings
-- ============================================================

CREATE TABLE IF NOT EXISTS redfin_sold (
    id              BIGSERIAL PRIMARY KEY,

    -- Address + location
    address         TEXT        NOT NULL,
    addr_key        TEXT        NOT NULL,
    city            TEXT,
    zip             TEXT,
    lat             DOUBLE PRECISION NOT NULL,
    lng             DOUBLE PRECISION NOT NULL,
    geom            GEOMETRY(Point, 4326),

    -- Sale details
    property_type   TEXT,
    sold_price      INTEGER,
    beds            SMALLINT,
    baths           SMALLINT,
    sqft            INTEGER,
    lot_sqft        INTEGER,
    yr_built        SMALLINT,
    dom             SMALLINT,
    price_per_sqft  INTEGER,
    sold_date       DATE,

    -- Source identifiers
    mls_num         TEXT,
    listing_url     TEXT        UNIQUE,

    -- Housekeeping
    fetched_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_redfin_sold_geom
    ON redfin_sold USING GIST(geom);

CREATE INDEX IF NOT EXISTS idx_redfin_sold_addr_key
    ON redfin_sold(addr_key);

CREATE INDEX IF NOT EXISTS idx_redfin_sold_sold_date
    ON redfin_sold(sold_date);

CREATE INDEX IF NOT EXISTS idx_redfin_sold_fetched_at
    ON redfin_sold(fetched_at);

COMMENT ON TABLE redfin_sold IS
    'Redfin recently-sold listings harvested by the DeepFin crawler. '
    'Upserted on listing_url; looks back sold_within_days per crawl config.';
