"""
SAP Material Stock API client — API_MATERIAL_STOCK_SRV (OData v2).

Fetches unrestricted warehouse stock (MatlWrhsStkQtyInMatlBaseUnit) from
Plants 1000 and 2000.  The OData $filter is plant-only to avoid SAP
silently truncating results with a complex ge/lt material-range filter.
Material-prefix filtering (A, C, I only) is applied in Python after the
full paginated fetch.

Heroku enforces a 30-second HTTP response deadline.  To work around this,
all SAP fetches run in a daemon background thread so the Flask route always
returns immediately from the in-memory cache while the thread populates it.

  fetch_stock()              → returns cache immediately (may be empty on
                                first call; triggers background refresh)
  fetch_stock(force_refresh) → same; always triggers a new background fetch
  cache_age_seconds()        → seconds since last successful fetch
  last_error()               → last fetch error string, or ''
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
_TIMEOUT   = 120  # seconds per individual HTTP request to SAP
_PAGE_SIZE = 1000 # $top per page — SAP OData v2 max is usually 1000

SAP_STOCK_CACHE_TTL_SECONDS = 300  # 5 minutes

# Material code first-letter whitelist — only codes starting with these
# letters are Indus part numbers and should appear in the SAP Recon tab.
_INDUS_PREFIXES = frozenset("ACI")

# ---------------------------------------------------------------------------
# In-memory cache + background-fetch state
# ---------------------------------------------------------------------------

_cache: Dict = {
    "data":       None,   # list[dict] — the merged rows
    "fetched_at": 0.0,    # monotonic epoch when cache was last populated
    "error":      None,   # last error string, or None
}

_fetch_lock   = threading.Lock()   # prevents duplicate concurrent fetches
_fetch_thread = None               # currently-running background thread (or None)


class SAPStockError(Exception):
    """Raised when the SAP stock fetch fails unrecoverably."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _auth() -> HTTPBasicAuth:
    return HTTPBasicAuth(_USERNAME, _PASSWORD)


def _odata_filter() -> str:
    """
    OData v2 $filter: Plants 1000 and 2000 only.

    Material-prefix filtering is intentionally NOT done here — the SAP OData
    layer silently truncates results when a complex ge/lt material range is
    combined with the plant filter.  Python-side filtering in _aggregate()
    is used instead to guarantee completeness.
    """
    return "(Plant eq '1000' or Plant eq '2000')"


def _fetch_all_pages() -> list:
    """
    Page through A_MatlStkInAcctMod and return every record as a raw dict.
    Runs entirely in a background thread so Heroku's 30-second HTTP timeout
    never applies to this function.
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

        next_link = body.get("d", {}).get("__next", None)
        if next_link:
            if next_link.startswith("/"):
                next_link = "https://my409486-api.s4hana.cloud.sap" + next_link
            if "$filter" not in next_link:
                logger.warning(
                    "Page %d: next_link missing $filter — data may be truncated.", page
                )
            url = next_link
        else:
            url = None
            logger.info("SAP stock fetch complete: %d total raw records", len(records))

        params = None  # next_link already carries all query params

    return records


def _aggregate(records: list) -> list:
    """
    Aggregate raw SAP records into one row per Material.
    Only keeps materials starting with A, C, or I (Indus prefixes).
    Returns a list of dicts sorted by material code.
    """
    agg: Dict[str, Dict] = {}

    for rec in records:
        mat   = (rec.get("Material") or "").strip().upper()
        plant = (rec.get("Plant")    or "").strip()
        if not mat:
            continue

        # Python-side prefix filter: keep A*, C*, I* only
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

    return sorted(agg.values(), key=lambda r: r["material"])


def _do_fetch_background():
    """
    Worker function that runs in a daemon thread.
    Fetches all pages from SAP, aggregates, and updates the in-memory cache.
    Errors are logged and stored in _cache['error'] but never propagated
    (the thread must not crash the dyno).
    """
    global _fetch_thread
    try:
        logger.info("SAP stock background fetch started")
        raw    = _fetch_all_pages()
        result = _aggregate(raw)
        _cache["data"]       = result
        _cache["fetched_at"] = time.monotonic()
        _cache["error"]      = None
        logger.info("SAP stock background fetch complete: %d materials", len(result))
    except Exception as exc:  # noqa: BLE001
        _cache["error"] = str(exc)
        logger.error("SAP stock background fetch failed: %s", exc)
    finally:
        with _fetch_lock:
            _fetch_thread = None


def _trigger_background_fetch():
    """
    Start a new background fetch thread if one is not already running.
    """
    global _fetch_thread
    with _fetch_lock:
        if _fetch_thread is not None and _fetch_thread.is_alive():
            logger.debug("SAP stock background fetch already in progress — skipping")
            return
        t = threading.Thread(target=_do_fetch_background, daemon=True,
                             name="sap-stock-fetch")
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
      - force_refresh=True (user clicked Refresh button), OR
      - cache is empty, OR
      - cache is older than SAP_STOCK_CACHE_TTL_SECONDS

    Returns immediately — never blocks on the SAP HTTP call, so Heroku's
    30-second response deadline is never breached.
    """
    now = time.monotonic()
    cache_stale = (
        _cache["data"] is None
        or (now - _cache["fetched_at"]) >= SAP_STOCK_CACHE_TTL_SECONDS
    )

    if force_refresh or cache_stale:
        _trigger_background_fetch()
    else:
        logger.debug("SAP stock: returning cached data (%d rows)", len(_cache["data"]))

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
    """Return True if a background fetch is currently in progress."""
    with _fetch_lock:
        return _fetch_thread is not None and _fetch_thread.is_alive()
