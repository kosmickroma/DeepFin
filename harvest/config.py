# harvest/config.py
#
# Role: Configuration — DB connection, area definitions, rate-limit settings.
#       Reads from environment variables (or .env file via python-dotenv).
#
# Connects to:
#   harvest/crawler.py    - imports get_db_conn(), CrawlConfig
#   scripts/*.py          - imports get_db_conn()

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db_conn():
    """Return a psycopg2 connection.

    Reads DATABASE_URL from environment. Falls back to individual vars
    (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD) for Cloud SQL TCP mode.
    """
    url = os.getenv("DATABASE_URL")
    if url:
        return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)

    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "lotledger"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


# ---------------------------------------------------------------------------
# Crawl configuration
# ---------------------------------------------------------------------------

@dataclass
class CrawlConfig:
    """Parameters controlling one crawl run."""

    # How many seconds to sleep between requests (can be fractional).
    # 1.5 s = ~40 req/min. Safe floor is ~0.5 s.
    delay_seconds: float = 1.5

    # ± fraction of delay to add as jitter (0.3 = ±30% of delay_seconds).
    jitter_fraction: float = 0.3

    # Grid cell size in degrees. 0.006° ≈ 0.4 miles wide.
    # Active listings: smaller cells (0.004) → more coverage per cell.
    # Sold listings:   larger cells (0.008) → fewer requests, sparser data.
    active_cell_deg: float = 0.004
    sold_cell_deg:   float = 0.008

    # How far back to pull sold listings on each cell.
    sold_within_days: int = 365

    # Max retries per cell on HTTP error before skipping.
    max_retries: int = 3

    # HTTP timeout (seconds).
    http_timeout: float = 20.0

    # After this many consecutive errors, pause for backoff_sleep_seconds.
    error_backoff_threshold: int = 5
    backoff_sleep_seconds: float = 60.0

    # Callable(n_completed, n_total) for progress reporting. Default: print.
    progress_fn: Callable[[int, int, str], None] = field(
        default_factory=lambda: _default_progress
    )


def _default_progress(n_done: int, n_total: int, label: str) -> None:
    pct = 100.0 * n_done / n_total if n_total else 0
    print(f"  [{n_done}/{n_total}  {pct:.1f}%]  {label}", flush=True)
