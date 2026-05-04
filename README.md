# DeepFin

Slow, resumable crawler that pulls **active (for-sale)** and **recently-sold** Redfin listings
for the Dallas–Fort Worth metro and stores them in the same PostgreSQL/PostGIS database as
[lot-ledger](../lot-ledger).

This repository is the standalone home for the scraper. It is intentionally separate from the
main LotLedger app so crawling, schema changes, and rate-limit tuning stay isolated.

---

## What It Does

1. **Divides** each county's bounding box into a grid of small cells (default ~0.4 miles wide).
2. **Queries** the unofficial Redfin GIS-CSV endpoint for each cell — once for active listings
   (`status=1`) and once for recently-sold (`status=8`, configurable lookback window).
3. **Parses** the response CSV: address, lat/lng, price, beds, baths, sqft, lot size, year built,
   MLS#, days-on-market, sold date, listing URL.
4. **Upserts** into `redfin_active` / `redfin_sold` tables (PostGIS points, GIST-indexed).
5. **Checkpoints** progress to a JSON state file so interrupted runs resume exactly where they
   stopped.

---

## Endpoint Discovery

The Redfin GIS-CSV endpoint used here is the same one the existing `api/redfin.py` in lot-ledger
uses for live draw-analysis pulls. The key difference is the `status` bitmask:

| `status` | Meaning          | SALE TYPE in CSV | SOLD DATE present? |
|----------|------------------|------------------|--------------------|
| `1`      | Active for-sale  | MLS Listing      | No                 |
| `8`      | Sold only        | PAST SALE        | Yes (Month-DD-YYYY)|
| `9`      | Active + Sold    | Both             | Sold rows only     |

Add `sold_within_days=N` to control the lookback window (90/180/365).

Every row also includes **LATITUDE** and **LONGITUDE** — no address matching needed for map
display. Address matching (`addr_key`) is still generated for parcel cross-referencing when you
click a lot in lot-ledger.

---

## Setup

### 1 — Install dependencies

```bash
cd deepfin
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2 — Configure database connection

```bash
cp .env.example .env
# Edit .env — set DATABASE_URL to point at the same Cloud SQL instance as lot-ledger
```

The tables land in the same DB as `parcels`, `tad_parcels`, etc. They don't interfere with any
existing tables.

### 3 — Create the tables

```bash
python scripts/setup_db.py
```

This runs `schema.sql` which creates:
- `redfin_active` — for-sale listings, UNIQUE on `listing_url`
- `redfin_sold`   — sold listings, UNIQUE on `listing_url`

Both have GIST spatial indexes on `geom` and B-tree indexes on `addr_key`.

---

## Running a Crawl

```bash
# Full Dallas County crawl — both active + sold (180-day lookback), 1.5s delay
python scripts/crawl_area.py dallas

# Sold data only, look back 1 year, slightly faster
python scripts/crawl_area.py dallas --mode sold --sold-days 365 --delay 1.0

# Active listings only, dry run (no DB writes) to estimate record counts
python scripts/crawl_area.py dallas --mode active --dry-run

# Start fresh (ignore checkpoint — re-crawl everything)
python scripts/crawl_area.py dallas --no-resume

# All four DFW counties:
python scripts/crawl_area.py dallas   --mode both
python scripts/crawl_area.py tarrant  --mode both
python scripts/crawl_area.py collin   --mode both
python scripts/crawl_area.py denton   --mode both
```

### Estimated crawl times (1.5s delay, full county)

| County  | Active cells | Active time | Sold cells | Sold time |
|---------|-------------|------------|-----------|----------|
| Dallas  | ~16,650      | ~7 hours   | ~4,162     | ~1.7 hrs  |
| Tarrant | ~21,962      | ~9 hours   | ~5,490     | ~2.3 hrs  |
| Collin  | ~26,896      | ~11 hours  | ~6,724     | ~2.8 hrs  |
| Denton  | ~29,340      | ~12 hours  | ~7,335     | ~3.1 hrs  |

Run in a `tmux` or `screen` session — the state checkpoint lets you pause and resume at any time.

### Incremental updates

After the initial full crawl, run incrementally with a shorter sold window:

```bash
# Weekly update — re-pull last 30 days of sold data across all counties
for area in dallas tarrant collin denton; do
    python scripts/crawl_area.py $area --mode sold --sold-days 30 --no-resume
done
```

Active listings are always fresh — every re-crawl overwrites price/status/DOM via upsert.

---

## Rate Limiting

The default config (`CrawlConfig`) uses:
- **1.5 seconds base delay** between requests
- **±30% jitter** (randomized so requests don't cluster)
- Effective rate: ~30 req/min
- **Auto-backoff**: if 5 consecutive errors occur, pause 60 seconds
- **Max 3 retries** per cell before skipping and marking complete

Reduce delay to `0.8` for faster crawls on a VPS; keep at `1.5+` if running on a shared
connection or if you see HTTP 429s.

---

## Lot-Ledger Integration Plan

### Phase 1 — Draw analysis (in-DB query, no live Redfin hit)

Replace the live per-draw Redfin fetch with a DB query against these tables:

```python
# api/main.py — instead of calling redfin.pull_grid()
# Query redfin_active WHERE ST_Within(geom, drawn_polygon)
# Query redfin_sold  WHERE ST_Within(geom, drawn_polygon) AND sold_date > NOW() - '6 months'
```

Benefits:
- Instant (DB is co-located) vs. 2–10s live Redfin fetch
- No Redfin rate-limit risk per user draw
- Sold comps available in the same sidebar as active listings

### Phase 2 — Browse layer sold overlay

New API endpoint:
```
GET /api/redfin/sold?min_lng=X&min_lat=X&max_lng=X&max_lat=X&days=180
→ GeoJSON FeatureCollection of sold point markers in viewport
```

Frontend: toggle button (like HOA toggle) — loads sold markers as a Leaflet `geoJSON` layer,
not PMTiles (too dynamic to bake into tiles). Purple/teal dot markers, popup with sold date,
price, beds/baths.

### Phase 3 — Parcel-detail popup enrichment

When user clicks any parcel in browse mode:
- Show most recent sold record matching `addr_key` (or ST_DWithin 30m centroid match)
- "Last sold: Apr 3 2026 — $369,900 (180 DOM)" in the parcel popup

### DB tables (already in this repo's schema.sql):

```sql
redfin_active  (geom, addr_key, price, status, dom, beds, baths, sqft, listing_url, …)
redfin_sold    (geom, addr_key, sold_price, sold_date, dom, beds, baths, sqft, listing_url, …)
```

These tables need to be created in the **lot-ledger Cloud SQL instance**:
```bash
DATABASE_URL=<cloud-sql-url> python scripts/setup_db.py
```

---

## Data Notes

- **Lat/lng from Redfin** are address-level geocodes (front door), not parcel centroids. They
  will fall inside the parcel polygon for most single-family properties. Condos may reference
  the building address rather than the unit.
- **Sold date format** in the CSV: `February-25-2026` — parsed to Python `date` in
  `harvest/redfin.py:_parse_sold_date()`.
- **Dedup key** is `listing_url`. Records without a URL (rare) are inserted with `ON CONFLICT
  DO NOTHING` to avoid duplicates.
- **Redfin ToS**: This tool is for private internal use only. Do not resell or redistribute data
  pulled by this crawler. The lot-ledger project and this crawler are internal tools for a single
  real estate acquisition team.
- **350 per cell cap**: Redfin returns at most 350 results per cell request. Cells in very dense
  condo areas may be undersampled. Shrink `--active-cell` to `0.002` if you suspect truncation.

---

## File Reference

| File | Purpose |
|------|---------|
| `harvest/redfin.py` | HTTP client — `fetch_cell_active()`, `fetch_cell_sold()`, address normalization |
| `harvest/crawler.py` | Crawl engine — grid generation, rate limiting, state checkpoint, DB upserts |
| `harvest/config.py` | `CrawlConfig` dataclass, `get_db_conn()` |
| `scripts/setup_db.py` | Applies `schema.sql` to create tables |
| `scripts/crawl_area.py` | CLI entry point — parse args, load area bbox, call `run_crawl()` |
| `schema.sql` | DDL for `redfin_active` and `redfin_sold` |
| `areas/*.json` | Bounding box + cell-size hints per county |
| `state/*.json` | Auto-generated checkpoint files (gitignored) |

---

## Quick-start Summary

```bash
# 1. Setup
cp .env.example .env        # add DATABASE_URL
python scripts/setup_db.py  # create tables

# 2. Smoke test — one small bbox, dry run
python scripts/crawl_area.py dallas --dry-run --mode both \
    --active-cell 0.02 --sold-cell 0.04   # big cells = fewer requests for testing

# 3. Full crawl (run in tmux)
python scripts/crawl_area.py dallas --mode both

# 4. Check results
psql $DATABASE_URL -c "SELECT COUNT(*) FROM redfin_active; SELECT COUNT(*) FROM redfin_sold;"
```
