#!/usr/bin/env python3
# scripts/crawl_area.py
#
# Role: Main CLI entry point for running a crawl pass over a named area.
#       Loads the area's bbox from areas/<name>.json, configures the crawl,
#       and calls crawler.run_crawl().
#
# Usage examples:
#   # Crawl Dallas — both active listings and sold data (default)
#   python scripts/crawl_area.py dallas
#
#   # Sold data only, 365-day lookback, resume from last checkpoint
#   python scripts/crawl_area.py dallas --mode sold --sold-days 365
#
#   # Active only, faster (0.8s delay), dry-run (no DB writes)
#   python scripts/crawl_area.py dallas --mode active --delay 0.8 --dry-run
#
#   # Start fresh (ignore saved state)
#   python scripts/crawl_area.py dallas --no-resume
#
#   # Reset state files and exit (next run starts over)
#   python scripts/crawl_area.py dallas --reset-state

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from harvest.config import CrawlConfig
from harvest.crawler import reset_state, run_crawl

AREAS_DIR = Path(__file__).parent.parent / "areas"


def load_area(name: str) -> dict:
    path = AREAS_DIR / f"{name}.json"
    if not path.exists():
        available = [p.stem for p in AREAS_DIR.glob("*.json")]
        print(f"Error: area '{name}' not found. Available: {available}")
        sys.exit(1)
    return json.loads(path.read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description="Harvest Redfin listings for a named area.")
    parser.add_argument("area", help="Area name (e.g. dallas, tarrant, collin, denton)")
    parser.add_argument(
        "--mode", choices=["active", "sold", "both"], default="both",
        help="Which data to fetch (default: both)",
    )
    parser.add_argument("--delay", type=float, default=1.5,
                        help="Seconds between requests (default: 1.5)")
    parser.add_argument("--sold-days", type=int, default=365,
                        help="Sold lookback window in days (default: 365)")
    parser.add_argument("--active-cell", type=float, default=0.004,
                        help="Grid cell size in degrees for active listings (default: 0.004)")
    parser.add_argument("--sold-cell", type=float, default=0.008,
                        help="Grid cell size in degrees for sold listings (default: 0.008)")
    parser.add_argument("--no-resume", action="store_true",
                        help="Start from scratch, ignoring saved state")
    parser.add_argument("--reset-state", action="store_true",
                        help="Delete state files and exit (next run starts fresh)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and print stats but do not write to DB")
    args = parser.parse_args()

    area_data = load_area(args.area)
    bbox = area_data["bbox"]

    if args.reset_state:
        reset_state(args.area, args.mode)
        print("State reset. Run again without --reset-state to start a fresh crawl.")
        return

    cfg = CrawlConfig(
        delay_seconds=args.delay,
        sold_within_days=args.sold_days,
        active_cell_deg=args.active_cell,
        sold_cell_deg=args.sold_cell,
    )

    print(f"\nStarting crawl: area={args.area}  mode={args.mode}  dry_run={args.dry_run}")
    print(f"BBox: lng [{bbox['min_lng']}, {bbox['max_lng']}]  "
          f"lat [{bbox['min_lat']}, {bbox['max_lat']}]")

    results = run_crawl(
        area_name=args.area,
        bbox=bbox,
        mode=args.mode,
        cfg=cfg,
        resume=not args.no_resume,
        dry_run=args.dry_run,
    )

    print("\n=== Final Summary ===")
    for mode_name, stats in results.items():
        print(f"  {mode_name}:")
        for k, v in stats.items():
            print(f"    {k}: {v}")


if __name__ == "__main__":
    main()
