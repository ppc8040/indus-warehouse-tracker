"""
SAP Material Stock API client — API_MATERIAL_STOCK_SRV (OData v2).

Fetches unrestricted warehouse stock (MatlWrhsStkQtyInMatlBaseUnit) from
Plants 1000 and 2000, restricted to Indus material codes starting with
A, C, or I (covers ACN*, AEW*, AJF*, AJM*, AHR*, CXX*, IXX*, etc.).

Results are cached in-memory for SAP_STOCK_CACHE_TTL_SECONDS (default 300).
Call fetch_stock(force_refresh=True) to bypass the cache on demand.
"""

import logging
import time
from typing import Dict

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BASE_URL  = "https://my409486-api.s4hana.cloud.sap/sap/opu/odata/sap/API_MATERIAL_STOCK_SRV"
_USERNAME  = "POWERBI_INTEGRATION"
_PASSWORD  = "d>xEX$dgfFjjw2=2<kbNkH-gPyu-+XnVJF%xJr)v"
_TIMEOUT   = 60   # seconds per HTTP request
_PAGE_SIZE = 1000 # $top per page — SAP OData v2 max is usually 1000

SAP_STOCK_CACHE_TTL_SECONDS = 300  # 5 minutes

# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

_cache: Dict = {
    "data":       None,   # list[dict] — the merged rows
    "fetched_at": 0.0,    # epoch seconds
    "error":      None,   # last error string, or None
}


class SAPStockError(Exception):
    """Raised when the SAP stock fetch fails unrecoverably."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _auth() -> HTTPBasicAuth:
    return HTTPBasicAuth(_USERNAME, _PASSWORD)


def _odata_filter() -> str:
    """
    OData v2 $filter for Plants 1000 and 2000, restricted to Indus material
    codes that start with A, C, or I.

    OData v2 does not support startswith() on all SAP systems, so we use
    ge/lt range pairs instead:
      A* → Material ge 'A' and Material lt 'B'
      C* → Material ge 'C' and Material lt 'D'
      I* → Material ge 'I' and Material lt 'J'
    """
    plant_filter = "(Plant eq '1000' or Plant eq '2000')"
    material_filter = (
        "(Material ge 'A' and Material lt 'B')"
        " or (Material ge 'C' and Material lt 'D')"
        " or (Material ge 'I' and Material lt 'J')"
    )
    return f"{plant_filter} and ({material_filter})"


def _fetch_all_pages() -> list:
    """
    Page through A_MatlStkInAcctMod and return every record as a raw dict.
    Uses __next-link OData v2 pagination. Absolute URL is enforced on every
    next_link to handle SAP returning relative paths.
    """
    url = f"{_BASE_URL}/A_MatlStkInAcctMod"
    params = {
        "$format": "json",
        "$select": "Material,Plant,MatlWrhsStkQtyInMatlBaseUnit",
        "$filter": _odata_filter(),
        "$top":    str(_PAGE_SIZE),
    }
    headers = {"Accept": "application/json"}
    auth    = _auth()
    records = []
    page    = 0

    while url:
        page += 1
        logger.info("SAP stock fetch: page %d url=%s", page, url)
        try:
            resp = requests.get(
                url, params=params if page == 1 else None,
                headers=headers, auth=auth, timeout=_TIMEOUT
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise SAPStockError(f"HTTP error on page {page}: {exc}") from exc

        try:
            body = resp.json()
        except ValueError as exc:
            raise SAPStockError(f"Invalid JSON on page {page}: {exc}") from exc

        results = body.get("d", {}).get("results", [])
        if not isinstance(results, list):
            raise SAPStockError(f"Unexpected response structure on page {page}")

        logger.info(
            "SAP stock fetch: page %d → %d records (total so far: %d)",
            page, len(results), len(records) + len(results)
        )
        records.extend(results)

        # OData v2 next-link lives in d.__next
        next_link = body.get("d", {}).get("__next", None)

        if next_link:
            # Ensure absolute URL — SAP sometimes returns a relative path
            if next_link.startswith("/"):
                next_link = "https://my409486-api.s4hana.cloud.sap" + next_link
            if "$filter" not in next_link:
                logger.warning(
                    "Page %d: next_link missing $filter param — data may be truncated.", page
                )
            url = next_link
        else:
            url = None
            logger.info("SAP stock fetch complete: %d total raw records", len(records))

        params = None  # next_link already contains all query params

    return records


def _aggregate(records: list) -> list:
    """
    Aggregate raw SAP records into one row per Material with:
      - sap_qty_1000: total MatlWrhsStkQtyInMatlBaseUnit for Plant 1000
      - sap_qty_2000: total MatlWrhsStkQtyInMatlBaseUnit for Plant 2000

    Returns a list of dicts sorted by material code.
    """
    agg: Dict[str, Dict] = {}

    for rec in records:
        mat   = (rec.get("Material") or "").strip().upper()
        plant = (rec.get("Plant")    or "").strip()
        if not mat:
            continue

        try:
            qty = float(rec.get("MatlWrhsStkQtyInMatlBaseUnit") or 0)
        except (TypeError, ValueError):
            qty = 0.0

        if mat not in agg:
            agg[mat] = {"material": mat, "sap_qty_1000": 0.0, "sap_qty_2000": 0.0}

        if plant == "1000":
            agg[mat]["sap_qty_1000"] += qty
        elif plant == "2000":
            agg[mat]["sap_qty_2000"] += qty

    return sorted(agg.values(), key=lambda r: r["material"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_stock(force_refresh: bool = False) -> list:
    """
    Return aggregated SAP stock as a list of dicts:
      [{"material": "ACN1375", "sap_qty_1000": 287.0, "sap_qty_2000": 0.0}, ...]

    Results are cached for SAP_STOCK_CACHE_TTL_SECONDS.
    Pass force_refresh=True to bypass the cache (used by the "Refresh" button).

    Raises SAPStockError on unrecoverable fetch failure.
    """
    now = time.monotonic()

    if (
        not force_refresh
        and _cache["data"] is not None
        and (now - _cache["fetched_at"]) < SAP_STOCK_CACHE_TTL_SECONDS
    ):
        logger.debug("SAP stock: returning cached data (%d rows)", len(_cache["data"]))
        return _cache["data"]

    try:
        raw     = _fetch_all_pages()
        result  = _aggregate(raw)
        _cache["data"]       = result
        _cache["fetched_at"] = time.monotonic()
        _cache["error"]      = None
        logger.info("SAP stock cache refreshed: %d materials", len(result))
        return result
    except SAPStockError as exc:
        _cache["error"] = str(exc)
        logger.error("SAP stock fetch failed: %s", exc)
        raise


def cache_age_seconds() -> float:
    """Return how many seconds ago the cache was last populated (0 if never)."""
    if _cache["fetched_at"] == 0.0:
        return 0.0
    return time.monotonic() - _cache["fetched_at"]


def last_error() -> str:
    """Return the last fetch error string, or empty string if none."""
    return _cache["error"] or ""
