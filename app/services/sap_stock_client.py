"""
SAP Material Stock API client — API_MATERIAL_STOCK_SRV (OData v2).

Fetches unrestricted warehouse stock (MatlWrhsStkQtyInMatlBaseUnit) for all
plants.  NO $filter is sent to SAP — this matches the reference BOM script
approach and prevents SAP from silently truncating or rejecting the request
when a filter is combined with large result sets.

All filtering is done in Python after the full paginated fetch:
  - Plant filter  : keep only Plant '1000' and '2000'
  - Material filter: keep only materials starting with A, C, or I (Indus prefixes)

Heroku enforces a 30-second HTTP response deadline.  To work around this,
all SAP fetches run in a daemon background thread so the Flask route always
returns immediately from the in-memory cache while the thread populates it.
The full fetch takes 2-5 minutes — the frontend polls /api/sap-recon-status
and shows a loading state until is_fetching=False and data is present.

  fetch_stock()              → returns cache immediately (may be empty on
                                first call; triggers background refresh)
  fetch_stock(force_refresh) → same; always triggers a new background fetch
  cache_age_seconds()        → seconds since last successful fetch
  last_error()               → last fetch error string, or ''
  is_fetching()              → True while a background fetch is in progress
"""

import logging
import threading
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
_TIMEOUT   = 180  # seconds per individual HTTP request to SAP (3 min — large pages)
_PAGE_SIZE = 500  # $top per page (matches reference script BATCHSIZE=500)

SAP_STOCK_CACHE_TTL_SECONDS = 300  # 5 minutes — re-fetch after cache expires

# Plants to keep after fetch (all others discarded in Python)
_VALID_PLANTS = frozenset({"1000", "2000"})

# Material code first-letter whitelist — only Indus part codes
_INDUS_PREFIXES = frozenset("ACI")

# ---------------------------------------------------------------------------
# In-memory cache + background-fetch state
# ---------------------------------------------------------------------------

_cache: Dict = {
    "data":       None,   # list[dict] — the aggregated rows
    "fetched_at": 0.0,    # monotonic time when cache was last populated
    "error":      None,   # last error string, or None
}

_fetch_lock   = threading.Lock()
_fetch_thread = None  # currently-running background thread (or None)


class SAPStockError(Exception):
    """Raised when the SAP stock fetch fails unrecoverably."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _auth() -> HTTPBasicAuth:
    return HTTPBasicAuth(_USERNAME, _PASSWORD)


def _fetch_all_pages() -> list:
    """
    Page through A_MatlStkInAcctMod using $skip/$top with NO $filter.
    Stops when a page returns fewer records than _PAGE_SIZE (last page).
    This matches the reference fetchmaterialstockdata() approach exactly.
    """
    endpoint = f"{_BASE_URL}/A_MatlStkInAcctMod"
    select_fields = (
        "Material,Plant,StorageLocation,Batch,"
        "InventorySpecialStockType,InventoryStockType,"
        "MaterialBaseUnit,MatlWrhsStkQtyInMatlBaseUnit"
    )
    headers = {"Accept": "application/json"}
    auth    = _auth()
    all_records = []
    skip = 0

    while True:
        params = {
            "$format": "json",
            "$select": select_fields,
            "$top":    str(_PAGE_SIZE),
            "$skip":   str(skip),
        }
        page_num = (skip // _PAGE_SIZE) + 1
        logger.info(
            "SAP stock fetch: page %d (skip=%d, top=%d)",
            page_num, skip, _PAGE_SIZE
        )
        try:
            resp = requests.get(
                endpoint, params=params,
                headers=headers, auth=auth, timeout=_TIMEOUT
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise SAPStockError(
                f"HTTP error on page {page_num} (skip={skip}): {exc}"
            ) from exc

        try:
            body = resp.json()
        except ValueError as exc:
            raise SAPStockError(
                f"Invalid JSON on page {page_num} (skip={skip}): {exc}"
            ) from exc

        results = body.get("d", {}).get("results", [])
        if not isinstance(results, list):
            raise SAPStockError(
                f"Unexpected response structure on page {page_num}: "
                f"'d.results' is not a list"
            )

        logger.info(
            "SAP stock fetch: page %d → %d records (total so far: %d)",
            page_num, len(results), len(all_records) + len(results)
        )
        all_records.extend(results)

        # Stop when we get fewer records than requested — this is the last page
        if len(results) < _PAGE_SIZE:
            logger.info(
                "SAP stock fetch complete: %d total raw records across %d pages",
                len(all_records), page_num
            )
            break

        skip += _PAGE_SIZE

    return all_records


def _aggregate(records: list) -> list:
    """
    Filter and aggregate raw SAP records into one row per Material.

    Filters applied (Python-side, after full fetch):
      1. Plant must be '1000' or '2000'
      2. Material must start with A, C, or I

    Aggregation: sum MatlWrhsStkQtyInMatlBaseUnit per plant per material.
    Returns list of dicts sorted by material code.
    """
    agg: Dict[str, Dict] = {}

    for rec in records:
        mat   = (rec.get("Material") or "").strip().upper()
        plant = (rec.get("Plant")    or "").strip()

        if not mat:
            continue

        # Plant filter: only 1000 and 2000
        if plant not in _VALID_PLANTS:
            continue

        # Material prefix filter: A, C, I only
        if mat[0] not in _INDUS_PREFIXES:
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

    result = sorted(agg.values(), key=lambda r: r["material"])
    logger.info(
        "SAP stock aggregate: %d raw records → %d Indus materials (Plants 1000+2000)",
        len(records), len(result)
    )
    return result


def _do_fetch_background():
    """
    Worker function running in a daemon thread.
    Fetches all pages, aggregates, and updates the in-memory cache.
    Errors are stored in cache['error'] but never propagated.
    """
    global _fetch_thread
    try:
        logger.info("SAP stock background fetch started (no $filter, full pagination)")
        raw    = _fetch_all_pages()
        result = _aggregate(raw)
        _cache["data"]       = result
        _cache["fetched_at"] = time.monotonic()
        _cache["error"]      = None
        logger.info(
            "SAP stock background fetch complete: %d materials cached",
            len(result)
        )
    except Exception as exc:  # noqa: BLE001
        _cache["error"] = str(exc)
        logger.error("SAP stock background fetch failed: %s", exc)
    finally:
        with _fetch_lock:
            _fetch_thread = None


def _trigger_background_fetch():
    """Start a background fetch thread if one is not already running."""
    global _fetch_thread
    with _fetch_lock:
        if _fetch_thread is not None and _fetch_thread.is_alive():
            logger.debug("SAP stock background fetch already running — skipping")
            return
        t = threading.Thread(
            target=_do_fetch_background,
            daemon=True,
            name="sap-stock-fetch",
        )
        _fetch_thread = t
        t.start()
        logger.info("SAP stock background fetch thread started")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_stock(force_refresh: bool = False) -> list:
    """
    Return currently-cached SAP stock (may be empty list on very first call
    while the background thread is still running).

    Always triggers a background refresh when:
      - force_refresh=True (user clicked Refresh), OR
      - cache is empty (first call), OR
      - cache is older than SAP_STOCK_CACHE_TTL_SECONDS

    Returns immediately — never blocks on the SAP HTTP call.
    """
    now = time.monotonic()
    cache_stale = (
        _cache["data"] is None
        or (now - _cache["fetched_at"]) >= SAP_STOCK_CACHE_TTL_SECONDS
    )

    if force_refresh or cache_stale:
        _trigger_background_fetch()
    else:
        logger.debug(
            "SAP stock: returning cached data (%d rows, age %.0fs)",
            len(_cache["data"]), now - _cache["fetched_at"]
        )

    return _cache["data"] or []


def cache_age_seconds() -> float:
    """Return how many seconds ago the cache was last populated (0 if never)."""
    if _cache["fetched_at"] == 0.0:
        return 0.0
    return time.monotonic() - _cache["fetched_at"]


def last_error() -> str:
    """Return the last fetch error string, or empty string if none."""
    return _cache["error"] or ""


def is_fetching() -> bool:
    """Return True if a background SAP fetch thread is currently running."""
    with _fetch_lock:
        return _fetch_thread is not None and _fetch_thread.is_alive()
