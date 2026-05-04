# harvest/redfin.py
#
# Role: Low-level Redfin GIS-CSV client. Fetches one grid cell at a time.
#       Returns structured dicts for active listings and recently-sold records.
#       Rate limiting and crawl orchestration live in crawler.py.
#
# Connects to:
#   harvest/crawler.py    - calls fetch_cell_active() and fetch_cell_sold()
#   harvest/config.py     - imports REDFIN_HEADERS, CITY_URL

from __future__ import annotations

import io
import re
from datetime import date
from typing import Any

import httpx
import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CITY_URL = "https://www.redfin.com/city/30794/TX/Dallas"
GIS_CSV_URL = "https://www.redfin.com/stingray/api/gis-csv"

# status bitmask values
STATUS_ACTIVE = "1"   # for-sale  (Active / Pending / Coming Soon)
STATUS_SOLD   = "8"   # past sale (Sold records only)

# Default window for sold lookback; callers can override.
DEFAULT_SOLD_DAYS = 180

REDFIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": CITY_URL,
}

# USPS suffix abbreviation → canonical form for address key normalization.
_SUFFIX_MAP: dict[str, str] = {
    "ALY": "ALLEY", "AVE": "AVENUE", "BND": "BEND", "BLVD": "BOULEVARD",
    "CIR": "CIRCLE", "CMN": "COMMON", "CRES": "CRESCENT", "CRST": "CREST",
    "CT": "COURT", "CV": "COVE", "DR": "DRIVE", "ESTS": "ESTATES",
    "EXPY": "EXPRESSWAY", "FLDS": "FIELDS", "FWY": "FREEWAY", "GLN": "GLEN",
    "GRV": "GROVE", "HOLW": "HOLLOW", "HTS": "HEIGHTS", "HWY": "HIGHWAY",
    "KNL": "KNOLL", "KNLS": "KNOLLS", "LN": "LANE", "MDW": "MEADOW",
    "MDWS": "MEADOWS", "MNR": "MANOR", "PKWY": "PARKWAY", "PL": "PLACE",
    "RD": "ROAD", "RDG": "RIDGE", "SQ": "SQUARE", "ST": "STREET",
    "TER": "TERRACE", "TERR": "TERRACE", "TRL": "TRAIL", "VLY": "VALLEY",
    "VW": "VIEW", "XING": "CROSSING",
}

_STRIP_SUFFIXES: frozenset[str] = frozenset({
    "ALLEY", "AVENUE", "BOULEVARD", "CIRCLE", "COMMON", "COURT", "COVE",
    "CROSSING", "DRIVE", "EXPRESSWAY", "FREEWAY", "HIGHWAY", "LANE",
    "PARKWAY", "PLACE", "ROAD", "SQUARE", "STREET", "TERRACE", "TRAIL", "WAY",
})

# Date formats Redfin uses in the SOLD DATE column, e.g. "February-25-2026"
_SOLD_DATE_RE = re.compile(
    r"^(?P<month>[A-Za-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})$"
)
_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_addr_key(raw: str) -> str:
    """Produce a canonical address key for cross-source matching.

    Uppercases, strips unit suffixes (#N), expands USPS abbreviations, and
    drops the trailing transport-type token so that "5609 REIGER AVE" and
    "5609 REIGER AVENUE" resolve to the same key.
    """
    text = str(raw or "").upper().strip().split("#")[0].strip()
    if not text:
        return ""
    tokens = [_SUFFIX_MAP.get(t, t) for t in text.split()]
    if tokens and tokens[-1] in _STRIP_SUFFIXES:
        tokens = tokens[:-1]
    return " ".join(tokens)


def _parse_sold_date(raw: str) -> date | None:
    """Parse Redfin's "February-25-2026" date format into a Python date."""
    if not raw or str(raw).strip().lower() in ("nan", "none", ""):
        return None
    m = _SOLD_DATE_RE.match(str(raw).strip())
    if not m:
        return None
    month_num = _MONTH_MAP.get(m.group("month").lower())
    if not month_num:
        return None
    try:
        return date(int(m.group("year")), month_num, int(m.group("day")))
    except ValueError:
        return None


def _to_int(val: Any) -> int | None:
    try:
        v = str(val or "").replace(",", "").strip()
        return int(float(v)) if v and v.lower() not in ("nan", "none", "") else None
    except (ValueError, TypeError):
        return None


def _to_float(val: Any) -> float | None:
    try:
        v = str(val or "").replace(",", "").strip()
        return float(v) if v and v.lower() not in ("nan", "none", "") else None
    except (ValueError, TypeError):
        return None


def _col(df: pd.DataFrame, *candidates: str) -> str | None:
    """Return the first column name that exists in the DataFrame."""
    for name in candidates:
        if name in df.columns:
            return name
    # Partial prefix match for URL column which has a long parenthetical
    for name in candidates:
        match = next((c for c in df.columns if c.startswith(name)), None)
        if match:
            return match
    return None


def _build_poly(min_lng: float, min_lat: float, max_lng: float, max_lat: float) -> str:
    return (
        f"{min_lng} {min_lat},"
        f"{max_lng} {min_lat},"
        f"{max_lng} {max_lat},"
        f"{min_lng} {max_lat},"
        f"{min_lng} {min_lat}"
    )


# ---------------------------------------------------------------------------
# Core fetch functions
# ---------------------------------------------------------------------------

def _base_params(status: str, poly: str) -> dict[str, str]:
    return {
        "al": "1",
        "market": "dallas",
        "mpt": "99",
        "num_homes": "350",
        "sf": "1,2,3,5,6,7",
        "start": "0",
        "status": status,
        "uipt": "1,2,3,4,5,6,7",
        "v": "8",
        "poly": poly,
    }


def _parse_active_df(df: pd.DataFrame) -> list[dict]:
    """Convert a raw active-listings DataFrame into structured dicts."""
    addr_col = _col(df, "ADDRESS", "Address")
    url_col  = _col(df, "URL")
    if not addr_col:
        return []

    records: list[dict] = []
    for _, row in df.iterrows():
        raw_addr = str(row.get(addr_col) or "").strip()
        if not raw_addr or raw_addr.upper() in ("NAN", "ADDRESS", ""):
            continue
        lat = _to_float(row.get("LATITUDE"))
        lng = _to_float(row.get("LONGITUDE"))
        if lat is None or lng is None:
            continue
        records.append({
            "kind": "active",
            "address": raw_addr,
            "addr_key": normalize_addr_key(raw_addr),
            "city": str(row.get("CITY") or "").strip() or None,
            "zip": str(row.get("ZIP OR POSTAL CODE") or "").strip() or None,
            "property_type": str(row.get("PROPERTY TYPE") or "").strip() or None,
            "price": _to_int(row.get("PRICE")),
            "beds": _to_int(row.get("BEDS")),
            "baths": _to_int(row.get("BATHS")),
            "sqft": _to_int(row.get("SQUARE FEET")),
            "lot_sqft": _to_int(row.get("LOT SIZE")),
            "yr_built": _to_int(row.get("YEAR BUILT")),
            "dom": _to_int(row.get("DAYS ON MARKET")),
            "price_per_sqft": _to_int(row.get("$/SQUARE FEET")),
            "hoa_monthly": _to_int(row.get("HOA/MONTH")),
            "status": str(row.get("STATUS") or "").strip() or None,
            "mls_num": str(row.get("MLS#") or "").strip() or None,
            "listing_url": str(row.get(url_col) or "").strip() or None if url_col else None,
            "lat": lat,
            "lng": lng,
        })
    return records


def _parse_sold_df(df: pd.DataFrame) -> list[dict]:
    """Convert a raw sold-listings DataFrame into structured dicts."""
    addr_col = _col(df, "ADDRESS", "Address")
    url_col  = _col(df, "URL")
    if not addr_col:
        return []

    records: list[dict] = []
    for _, row in df.iterrows():
        raw_addr = str(row.get(addr_col) or "").strip()
        if not raw_addr or raw_addr.upper() in ("NAN", "ADDRESS", ""):
            continue
        lat = _to_float(row.get("LATITUDE"))
        lng = _to_float(row.get("LONGITUDE"))
        if lat is None or lng is None:
            continue
        # Only keep PAST SALE records (skip any Active/Pending that leaked in)
        sale_type = str(row.get("SALE TYPE") or "").upper()
        if "PAST" not in sale_type and "SOLD" not in sale_type:
            continue
        records.append({
            "kind": "sold",
            "address": raw_addr,
            "addr_key": normalize_addr_key(raw_addr),
            "city": str(row.get("CITY") or "").strip() or None,
            "zip": str(row.get("ZIP OR POSTAL CODE") or "").strip() or None,
            "property_type": str(row.get("PROPERTY TYPE") or "").strip() or None,
            "sold_price": _to_int(row.get("PRICE")),
            "beds": _to_int(row.get("BEDS")),
            "baths": _to_int(row.get("BATHS")),
            "sqft": _to_int(row.get("SQUARE FEET")),
            "lot_sqft": _to_int(row.get("LOT SIZE")),
            "yr_built": _to_int(row.get("YEAR BUILT")),
            "dom": _to_int(row.get("DAYS ON MARKET")),
            "price_per_sqft": _to_int(row.get("$/SQUARE FEET")),
            "sold_date": _parse_sold_date(row.get("SOLD DATE")),
            "mls_num": str(row.get("MLS#") or "").strip() or None,
            "listing_url": str(row.get(url_col) or "").strip() or None if url_col else None,
            "lat": lat,
            "lng": lng,
        })
    return records


def fetch_cell_active(
    client: httpx.Client,
    min_lng: float,
    min_lat: float,
    max_lng: float,
    max_lat: float,
) -> list[dict]:
    """Fetch active (for-sale) listings for one grid cell. Returns list of dicts.

    Raises httpx.HTTPError on network failures — caller should handle.
    Returns [] on empty or invalid response.
    """
    poly = _build_poly(min_lng, min_lat, max_lng, max_lat)
    params = _base_params(STATUS_ACTIVE, poly)
    resp = client.get(GIS_CSV_URL, params=params)
    resp.raise_for_status()
    if len(resp.text) < 100 or resp.text.lstrip().startswith("{"):
        return []
    df = pd.read_csv(io.StringIO(resp.text))
    return _parse_active_df(df)


def fetch_cell_sold(
    client: httpx.Client,
    min_lng: float,
    min_lat: float,
    max_lng: float,
    max_lat: float,
    sold_within_days: int = DEFAULT_SOLD_DAYS,
) -> list[dict]:
    """Fetch recently-sold listings for one grid cell. Returns list of dicts.

    sold_within_days: how far back to look (90=3mo, 180=6mo, 365=1yr).
    Raises httpx.HTTPError on network failures — caller should handle.
    Returns [] on empty or invalid response.
    """
    poly = _build_poly(min_lng, min_lat, max_lng, max_lat)
    params = _base_params(STATUS_SOLD, poly)
    params["sold_within_days"] = str(sold_within_days)
    resp = client.get(GIS_CSV_URL, params=params)
    resp.raise_for_status()
    if len(resp.text) < 100 or resp.text.lstrip().startswith("{"):
        return []
    df = pd.read_csv(io.StringIO(resp.text))
    return _parse_sold_df(df)


def prime_session(client: httpx.Client) -> None:
    """Load the Redfin city page once to establish cookies/session context."""
    try:
        client.get(CITY_URL)
    except httpx.HTTPError:
        pass
