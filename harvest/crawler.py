# harvest/crawler.py
#
# Role: Slow-crawl engine. Iterates a bounding-box grid cell-by-cell, fetches
#       active and/or sold listings, upserts into the DB, and persists state
#       so interrupted runs resume where they left off.
#
# Connects to:
#   harvest/redfin.py     - fetch_cell_active(), fetch_cell_sold(), prime_session()
#   harvest/config.py     - CrawlConfig, get_db_conn()
#   scripts/crawl_area.py - calls run_crawl()

from __future__ import annotations

import json
import math
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
import psycopg2

from harvest.config import CrawlConfig, get_db_conn
from harvest.redfin import (
    REDFIN_HEADERS,
    fetch_cell_active,
    fetch_cell_sold,
    prime_session,
)

# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------

def _state_path(area_name: str, mode: str) -> Path:
    """Return path to the JSON state file for this area + mode."""
    here = Path(__file__).parent.parent / "state"
    here.mkdir(exist_ok=True)
    return here / f"{area_name}_{mode}.json"


def _load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"completed_cells": [], "started_at": None, "last_updated": None}


def _save_state(path: Path, state: dict) -> None:
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(state, indent=2, default=str))


# ---------------------------------------------------------------------------
# Grid generation
# ---------------------------------------------------------------------------

def _build_grid(
    min_lng: float,
    min_lat: float,
    max_lng: float,
    max_lat: float,
    cell_deg: float,
) -> list[tuple[float, float, float, float]]:
    """Return list of (min_lng, min_lat, max_lng, max_lat) cell tuples."""
    cells = []
    lat = min_lat
    while lat < max_lat:
        lng = min_lng
        while lng < max_lng:
            cells.append((
                round(lng, 6),
                round(lat, 6),
                round(min(lng + cell_deg, max_lng), 6),
                round(min(lat + cell_deg, max_lat), 6),
            ))
            lng += cell_deg
        lat += cell_deg
    return cells


def _cell_key(cell: tuple[float, float, float, float]) -> str:
    return f"{cell[0]},{cell[1]},{cell[2]},{cell[3]}"


# ---------------------------------------------------------------------------
# DB upserts
# ---------------------------------------------------------------------------

_UPSERT_ACTIVE = """
INSERT INTO redfin_active (
    address, addr_key, city, zip, property_type,
    price, beds, baths, sqft, lot_sqft, yr_built,
    dom, price_per_sqft, hoa_monthly, status,
    mls_num, listing_url, lat, lng,
    geom, fetched_at
) VALUES (
    %(address)s, %(addr_key)s, %(city)s, %(zip)s, %(property_type)s,
    %(price)s, %(beds)s, %(baths)s, %(sqft)s, %(lot_sqft)s, %(yr_built)s,
    %(dom)s, %(price_per_sqft)s, %(hoa_monthly)s, %(status)s,
    %(mls_num)s, %(listing_url)s, %(lat)s, %(lng)s,
    ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326),
    NOW()
)
ON CONFLICT (listing_url) DO UPDATE SET
    price          = EXCLUDED.price,
    status         = EXCLUDED.status,
    dom            = EXCLUDED.dom,
    hoa_monthly    = EXCLUDED.hoa_monthly,
    price_per_sqft = EXCLUDED.price_per_sqft,
    geom           = EXCLUDED.geom,
    fetched_at     = EXCLUDED.fetched_at
WHERE redfin_active.listing_url IS NOT NULL
"""

_UPSERT_ACTIVE_NO_URL = """
INSERT INTO redfin_active (
    address, addr_key, city, zip, property_type,
    price, beds, baths, sqft, lot_sqft, yr_built,
    dom, price_per_sqft, hoa_monthly, status,
    mls_num, listing_url, lat, lng,
    geom, fetched_at
) VALUES (
    %(address)s, %(addr_key)s, %(city)s, %(zip)s, %(property_type)s,
    %(price)s, %(beds)s, %(baths)s, %(sqft)s, %(lot_sqft)s, %(yr_built)s,
    %(dom)s, %(price_per_sqft)s, %(hoa_monthly)s, %(status)s,
    %(mls_num)s, %(listing_url)s, %(lat)s, %(lng)s,
    ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326),
    NOW()
)
ON CONFLICT DO NOTHING
"""

_UPSERT_SOLD = """
INSERT INTO redfin_sold (
    address, addr_key, city, zip, property_type,
    sold_price, beds, baths, sqft, lot_sqft, yr_built,
    dom, price_per_sqft, sold_date,
    mls_num, listing_url, lat, lng,
    geom, fetched_at
) VALUES (
    %(address)s, %(addr_key)s, %(city)s, %(zip)s, %(property_type)s,
    %(sold_price)s, %(beds)s, %(baths)s, %(sqft)s, %(lot_sqft)s, %(yr_built)s,
    %(dom)s, %(price_per_sqft)s, %(sold_date)s,
    %(mls_num)s, %(listing_url)s, %(lat)s, %(lng)s,
    ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326),
    NOW()
)
ON CONFLICT (listing_url) DO UPDATE SET
    sold_price     = EXCLUDED.sold_price,
    sold_date      = EXCLUDED.sold_date,
    dom            = EXCLUDED.dom,
    price_per_sqft = EXCLUDED.price_per_sqft,
    geom           = EXCLUDED.geom,
    fetched_at     = EXCLUDED.fetched_at
WHERE redfin_sold.listing_url IS NOT NULL
"""

_UPSERT_SOLD_NO_URL = """
INSERT INTO redfin_sold (
    address, addr_key, city, zip, property_type,
    sold_price, beds, baths, sqft, lot_sqft, yr_built,
    dom, price_per_sqft, sold_date,
    mls_num, listing_url, lat, lng,
    geom, fetched_at
) VALUES (
    %(address)s, %(addr_key)s, %(city)s, %(zip)s, %(property_type)s,
    %(sold_price)s, %(beds)s, %(baths)s, %(sqft)s, %(lot_sqft)s, %(yr_built)s,
    %(dom)s, %(price_per_sqft)s, %(sold_date)s,
    %(mls_num)s, %(listing_url)s, %(lat)s, %(lng)s,
    ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326),
    NOW()
)
ON CONFLICT DO NOTHING
"""


def _upsert_records(cur, records: list[dict], mode: Literal["active", "sold"]) -> int:
    """Write records to DB; returns count inserted/updated."""
    count = 0
    for rec in records:
        url = rec.get("listing_url") or ""
        if mode == "active":
            sql = _UPSERT_ACTIVE if url else _UPSERT_ACTIVE_NO_URL
        else:
            sql = _UPSERT_SOLD if url else _UPSERT_SOLD_NO_URL
        cur.execute(sql, rec)
        count += 1
    return count


# ---------------------------------------------------------------------------
# Main crawl function
# ---------------------------------------------------------------------------

def run_crawl(
    area_name: str,
    bbox: dict,
    mode: Literal["active", "sold", "both"] = "both",
    cfg: CrawlConfig | None = None,
    resume: bool = True,
    dry_run: bool = False,
) -> dict:
    """Crawl a bounding-box area and upsert results into the DB.

    Args:
        area_name:  Identifier used in state file names (e.g. "dallas").
        bbox:       {"min_lng": float, "min_lat": float,
                     "max_lng": float, "max_lat": float}
        mode:       "active", "sold", or "both".
        cfg:        CrawlConfig instance; defaults created if None.
        resume:     If True, skip already-completed cells.
        dry_run:    Fetch and parse but do not write to DB.

    Returns:
        Summary dict with counts and timing.
    """
    if cfg is None:
        cfg = CrawlConfig()

    modes: list[str] = (
        ["active", "sold"] if mode == "both"
        else [mode]
    )

    results: dict[str, dict] = {}

    for m in modes:
        cell_deg = cfg.active_cell_deg if m == "active" else cfg.sold_cell_deg
        state_path = _state_path(area_name, m)
        state = _load_state(state_path) if resume else {"completed_cells": []}
        if state["started_at"] is None:
            state["started_at"] = datetime.now(timezone.utc).isoformat()

        completed = set(state["completed_cells"])
        grid = _build_grid(
            bbox["min_lng"], bbox["min_lat"],
            bbox["max_lng"], bbox["max_lat"],
            cell_deg,
        )
        pending = [c for c in grid if _cell_key(c) not in completed]

        print(
            f"\n=== {area_name.upper()} / {m.upper()} ==="
            f"\n  Total cells: {len(grid)} | Completed: {len(completed)} | Pending: {len(pending)}"
            f"\n  Cell size: {cell_deg}°  |  Delay: {cfg.delay_seconds}s ±{cfg.jitter_fraction*100:.0f}%"
            f"\n  dry_run={dry_run}",
            flush=True,
        )

        total_fetched = 0
        total_written = 0
        consecutive_errors = 0
        t_start = time.time()

        conn = None if dry_run else get_db_conn()

        timeout = httpx.Timeout(cfg.http_timeout)
        with httpx.Client(headers=REDFIN_HEADERS, timeout=timeout, follow_redirects=True) as client:
            prime_session(client)

            for i, cell in enumerate(pending):
                # Progress reporting
                cfg.progress_fn(len(completed), len(grid), f"{m} {cell}")

                # Rate-limit: delay + jitter
                if i > 0:
                    jitter = random.uniform(-cfg.jitter_fraction, cfg.jitter_fraction)
                    sleep_s = max(0.2, cfg.delay_seconds * (1 + jitter))
                    time.sleep(sleep_s)

                # Fetch with retry
                records: list[dict] = []
                for attempt in range(1, cfg.max_retries + 1):
                    try:
                        if m == "active":
                            records = fetch_cell_active(client, *cell)
                        else:
                            records = fetch_cell_sold(
                                client, *cell,
                                sold_within_days=cfg.sold_within_days,
                            )
                        consecutive_errors = 0
                        break
                    except httpx.HTTPStatusError as e:
                        print(f"    HTTP {e.response.status_code} on attempt {attempt}/{cfg.max_retries}", flush=True)
                        if attempt < cfg.max_retries:
                            time.sleep(cfg.delay_seconds * 3)
                    except httpx.HTTPError as e:
                        print(f"    Network error attempt {attempt}: {e}", flush=True)
                        if attempt < cfg.max_retries:
                            time.sleep(cfg.delay_seconds * 2)
                    except Exception as e:
                        print(f"    Unexpected error attempt {attempt}: {e}", flush=True)
                        break
                else:
                    consecutive_errors += 1
                    print(f"    Skipping cell after {cfg.max_retries} retries. consecutive_errors={consecutive_errors}", flush=True)
                    if consecutive_errors >= cfg.error_backoff_threshold:
                        print(f"    {consecutive_errors} consecutive errors — backing off {cfg.backoff_sleep_seconds}s", flush=True)
                        time.sleep(cfg.backoff_sleep_seconds)
                        consecutive_errors = 0
                    # Mark as completed anyway to avoid infinite retry on bad cells
                    completed.add(_cell_key(cell))
                    state["completed_cells"] = list(completed)
                    _save_state(state_path, state)
                    continue

                total_fetched += len(records)

                # Write to DB
                if records and not dry_run and conn is not None:
                    try:
                        with conn.cursor() as cur:
                            written = _upsert_records(cur, records, m)
                            total_written += written
                        conn.commit()
                    except psycopg2.Error as e:
                        print(f"    DB error: {e}", flush=True)
                        conn.rollback()

                # Checkpoint state
                completed.add(_cell_key(cell))
                state["completed_cells"] = list(completed)
                _save_state(state_path, state)

        if conn is not None:
            conn.close()

        elapsed = time.time() - t_start
        results[m] = {
            "cells_total": len(grid),
            "cells_processed": len(pending),
            "records_fetched": total_fetched,
            "records_written": total_written,
            "elapsed_seconds": round(elapsed, 1),
        }
        print(
            f"\n  Done {m}: {len(pending)} cells | {total_fetched} fetched | "
            f"{total_written} written | {elapsed:.0f}s elapsed",
            flush=True,
        )

    return results


def reset_state(area_name: str, mode: Literal["active", "sold", "both"] = "both") -> None:
    """Delete state files so the next run starts from scratch."""
    modes = ["active", "sold"] if mode == "both" else [mode]
    for m in modes:
        p = _state_path(area_name, m)
        if p.exists():
            p.unlink()
            print(f"Deleted state: {p}")
