#!/usr/bin/env python3
# scripts/setup_db.py
#
# Role: One-time setup — runs schema.sql against the target database to create
#       redfin_active and redfin_sold tables.
#
# Usage:
#   DATABASE_URL=postgresql://... python scripts/setup_db.py
#   python scripts/setup_db.py          # reads DATABASE_URL from .env

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from harvest.config import get_db_conn

SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"


def main() -> None:
    sql = SCHEMA_PATH.read_text()

    print(f"Connecting to database…")
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print("Schema applied successfully.")
        print("  Tables: redfin_active, redfin_sold")
        print("  Indexes: geom (GIST), addr_key, status/sold_date, fetched_at")

        # Quick row count to confirm
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM redfin_active")
            active_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM redfin_sold")
            sold_count = cur.fetchone()[0]
        print(f"\nCurrent rows: redfin_active={active_count}, redfin_sold={sold_count}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
