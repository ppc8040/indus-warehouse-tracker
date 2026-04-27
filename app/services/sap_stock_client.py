"""
app/services/sap_stock_client.py

SAP Material Stock client — API_MATERIAL_STOCK_SRV (OData v2).

FLOW (as described in the reference BOM script fetch_material_stock_data):
  1. Fetch ALL pages from A_MatlStkInAcctMod (no server-side $filter).
     Only 2 fields are selected: Material, MatlWrhsStkQtyInMatlBaseUnit.
     We also select Plant so we can filter it in Python.
  2. Python plant filter : keep only Plant '1000' and '2000'.
  3. Python material filter: keep only materials starting with A, C, or I.
  4. Aggregate qty per material per plant → one dict per unique material.

WHY no server-side $filter for plant:
  SAP OData implementations often silently truncate paginated results
  when a $filter is combined with $skip/$top on large entity sets.
  Fetching everything and filtering in Python is safer and matches
  the pattern used in the reference BOM script.

HEROKU / TIMEOUT:
  The full fetch takes 2-5 minutes.  Flask HTTP requests on Heroku must
  respond within 30 seconds — the SAP call MUST run in a background thread.
  The Flask routes return immediately from cache; the frontend polls
  /admin/api/sap-recon-status every 5 seconds until is_fetching()==False.

PUBLIC API:
  fetch_stock(force_refresh=False) → list[dict]   (returns cache immediately)
  is_fetching()                    → bool
  cache_age_seconds()              → float
  last_error()                     → str
  live_record_count()              → int  (records fetched so far THIS run)
"""

import logging
import os
import threading
import time
from typing import Dict, List, Optional

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration  — credentials identical to the reference BOM script
# ---------------------------------------------------------------------------

_BASE_URL  = "https://my409486-api.s4hana.cloud.sap/sap/opu/odata/sap/API_MATERIAL_STOCK_SRV"
_USERNAME  = "POWERBI_INTEGRATION"
_PASSWORD  = "d>xEX$dgfFjjw2=2<kbNkH-gPyu-+XnVJF%xJr)v"

# Per-page request timeout (seconds).  Reference script uses 180; we use 300
# so a slow SAP page doesn't abort the whole fetch unnecessarily.
_TIMEOUT_PER_PAGE = 300

# Page size: 500 rows matches the reference BOM script's batch_size=500.
# Smaller pages are more reliable on slow SAP connections.
_PAGE_SIZE = 500

# Cache time-to-live: 5 minutes.  After this the next fetch_stock() call
# will kick off a new background refresh automatically.
_CACHE_TTL = 300

# Plant codes to keep (Python-side filter applied after full fetch).
_VALID_PLANTS = frozenset({"1000", "2000"})

# Material first-character whitelist — Indus part prefixes.
_INDUS_PREFIXES = frozenset("ACI")

# Only fetch these 3 fields — slimmer payload, faster pages.
# Material + Plant for filtering; MatlWrhsStkQtyInMatlBaseUnit for qty.
_SELECT = "Material,Plant,MatlWrhsStkQtyInMatlBaseUnit"

# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

_cache: Dict = {
    "data":         None,   # list[dict] — aggregated result rows
    "fetched_at":   0.0,    # monotonic timestamp of last successful fetch
    "error":        None,   # str | None — last unrecoverable error message
    "_fetching":    False,  # bool — True while background thread is running
    "_live_count":  0,      # int — raw records fetched SO FAR in current run
}

_lock         = threading.Lock()
_fetch_thread: Optional[threading.Thread] = None


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class SAPStockError(Exception):
    """Raised when the SAP stock fetch fails unrecoverably."""


# ---------------------------------------------------------------------------
# Core paginated fetch  (mirrors fetch_material_stock_data in reference script)
# ---------------------------------------------------------------------------

def _fetch_all_raw_records() -> List[dict]:
    """
    Page through A_MatlStkInAcctMod with $skip/$top.

    No $filter is sent to SAP — we fetch the entire entity set and
    apply plant + material filters in Python afterward.

    Termination conditions (same logic as reference script):
      - Page returns fewer rows than _PAGE_SIZE  → last page reached
      - Page returns 0 rows                      → nothing more to fetch
      - HTTP / JSON error                        → raise SAPStockError

    Returns: flat list of raw SAP dicts.
    Raises:  SAPStockError on unrecoverable failure.
    """
    endpoint = f"{_BASE_URL}/A_MatlStkInAcctMod"
    auth     = HTTPBasicAuth(_USERNAME, _PASSWORD)
    headers  = {"Accept": "application/json"}

    all_records: List[dict] = []
    skip = 0
    page_num = 0

    print(f"[SAP STOCK] ===== Starting full paginated fetch =====")
    print(f"[SAP STOCK] Endpoint : {endpoint}")
    print(f"[SAP STOCK] Page size: {_PAGE_SIZE}")
    print(f"[SAP STOCK] Fields   : {_SELECT}")
    print(f"[SAP STOCK] NOTE     : No server-side $filter — plant/material filter applied in Python")

    while True:
        page_num += 1
        params = {
            "$format": "json",
            "$select": _SELECT,
            "$top":    str(_PAGE_SIZE),
            "$skip":   str(skip),
        }

        print(f"[SAP STOCK] Page {page_num:>4d} | skip={skip:>8d} | cumulative records so far: {len(all_records):>7d}")
        logger.info(
            "SAP stock page %d: skip=%d, top=%d | cumulative=%d",
            page_num, skip, _PAGE_SIZE, len(all_records)
        )

        # ── HTTP request ──────────────────────────────────────────────────
        try:
            resp = requests.get(
                endpoint,
                params=params,
                headers=headers,
                auth=auth,
                timeout=_TIMEOUT_PER_PAGE,
            )
            resp.raise_for_status()

        except requests.Timeout:
            msg = (
                f"Timeout on page {page_num} (skip={skip}) after "
                f"{len(all_records)} records. Increase _TIMEOUT_PER_PAGE or reduce _PAGE_SIZE."
            )
            print(f"[SAP STOCK] ERROR: {msg}")
            logger.error("SAP stock: %s", msg)
            raise SAPStockError(msg) from None

        except requests.RequestException as exc:
            msg = f"HTTP error on page {page_num} (skip={skip}): {exc}"
            print(f"[SAP STOCK] ERROR: {msg}")
            logger.error("SAP stock: %s", msg)
            raise SAPStockError(msg) from exc

        # ── Parse JSON ────────────────────────────────────────────────────
        try:
            body = resp.json()
        except ValueError as exc:
            msg = f"Invalid JSON on page {page_num} (skip={skip}): {exc}"
            print(f"[SAP STOCK] ERROR: {msg}")
            logger.error("SAP stock: %s", msg)
            raise SAPStockError(msg) from exc

        results = body.get("d", {}).get("results", [])
        if not isinstance(results, list):
            msg = f"Unexpected response structure on page {page_num}: 'd.results' is not a list"
            print(f"[SAP STOCK] ERROR: {msg}")
            raise SAPStockError(msg)

        rows_this_page = len(results)
        all_records.extend(results)

        # ── Update live count so the status endpoint can show progress ────
        with _lock:
            _cache["_live_count"] = len(all_records)

        print(
            f"[SAP STOCK] Page {page_num:>4d} returned {rows_this_page:>5d} rows "
            f"| running total: {len(all_records):>7d}"
        )
        logger.info(
            "SAP stock page %d: %d rows received | running total: %d",
            page_num, rows_this_page, len(all_records)
        )

        # ── Termination ───────────────────────────────────────────────────
        if rows_this_page == 0:
            print(f"[SAP STOCK] Page {page_num} returned 0 rows — fetch complete.")
            logger.info("SAP stock: 0-row page at skip=%d — fetch complete.", skip)
            break

        if rows_this_page < _PAGE_SIZE:
            print(
                f"[SAP STOCK] Page {page_num} returned {rows_this_page} rows "
                f"(< page_size {_PAGE_SIZE}) — last page reached, fetch complete."
            )
            logger.info(
                "SAP stock: last page (page %d, %d rows < %d) — fetch complete.",
                page_num, rows_this_page, _PAGE_SIZE
            )
            break

        skip += _PAGE_SIZE

    print(f"[SAP STOCK] ===== Raw fetch complete: {len(all_records)} total records from {page_num} pages =====")
    logger.info("SAP stock raw fetch complete: %d records in %d pages", len(all_records), page_num)
    return all_records


# ---------------------------------------------------------------------------
# Filter + aggregate  (mirrors create_mb52_stock_dict in reference script)
# ---------------------------------------------------------------------------

def _filter_and_aggregate(raw_records: List[dict]) -> List[dict]:
    """
    Step 1 — Plant filter  : keep only Plant '1000' and '2000'.
    Step 2 — Material filter: keep only materials starting with A, C, or I.
    Step 3 — Aggregate     : sum qty per material per plant.

    Returns list of dicts sorted by material code:
      [
        {"material": "ACN1375", "sap_qty_1000": 287.0, "sap_qty_2000": 0.0},
        ...
      ]
    """
    print(f"[SAP STOCK] ===== Starting filter + aggregate on {len(raw_records)} raw records =====")
    logger.info("SAP stock aggregate: processing %d raw records", len(raw_records))

    # ── Step 1: Plant filter ──────────────────────────────────────────────
    plant_filtered: List[dict] = []
    skipped_plant = 0
    for rec in raw_records:
        plant = (rec.get("Plant") or "").strip()
        if plant in _VALID_PLANTS:
            plant_filtered.append(rec)
        else:
            skipped_plant += 1

    print(
        f"[SAP STOCK] Plant filter : kept {len(plant_filtered):>7d} records "
        f"(Plant 1000 or 2000) | discarded {skipped_plant:>7d} other plants"
    )
    logger.info(
        "SAP stock plant filter: %d kept (1000/2000), %d discarded",
        len(plant_filtered), skipped_plant
    )

    # ── Step 2: Material prefix filter ────────────────────────────────────
    mat_filtered: List[dict] = []
    skipped_mat = 0
    for rec in plant_filtered:
        mat = (rec.get("Material") or "").strip().upper()
        if mat and mat[0] in _INDUS_PREFIXES:
            mat_filtered.append(rec)
        else:
            skipped_mat += 1

    print(
        f"[SAP STOCK] Material filter: kept {len(mat_filtered):>7d} records "
        f"(starting A/C/I) | discarded {skipped_mat:>7d} non-Indus materials"
    )
    logger.info(
        "SAP stock material filter: %d kept (A/C/I prefix), %d discarded",
        len(mat_filtered), skipped_mat
    )

    # ── Step 3: Aggregate qty per material per plant ───────────────────────
    agg: Dict[str, Dict] = {}
    parse_errors = 0

    for rec in mat_filtered:
        mat   = (rec.get("Material") or "").strip().upper()
        plant = (rec.get("Plant")    or "").strip()

        try:
            qty = float(rec.get("MatlWrhsStkQtyInMatlBaseUnit") or 0)
        except (TypeError, ValueError):
            qty = 0.0
            parse_errors += 1

        if mat not in agg:
            agg[mat] = {"material": mat, "sap_qty_1000": 0.0, "sap_qty_2000": 0.0}

        if plant == "1000":
            agg[mat]["sap_qty_1000"] += qty
        elif plant == "2000":
            agg[mat]["sap_qty_2000"] += qty

    result = sorted(agg.values(), key=lambda r: r["material"])

    if parse_errors:
        print(f"[SAP STOCK] WARNING: {parse_errors} records had unparseable qty values (treated as 0)")
        logger.warning("SAP stock: %d qty parse errors (treated as 0)", parse_errors)

    print(
        f"[SAP STOCK] Aggregate result: {len(result)} unique Indus materials "
        f"across Plants 1000 + 2000"
    )
    logger.info(
        "SAP stock aggregate complete: %d raw → %d unique materials",
        len(raw_records), len(result)
    )
    return result


# ---------------------------------------------------------------------------
# Background fetch worker
# ---------------------------------------------------------------------------

def _do_fetch_background():
    """
    Worker that runs in a daemon thread.
    Fetches all raw records, applies filters, aggregates, and writes to cache.
    Errors are stored in cache['error'] — they are never propagated outward.

    The _fetching flag is set to True BEFORE this thread starts (inside
    _trigger_background_fetch under the lock) so is_fetching() returns True
    immediately, with zero race window.
    """
    global _fetch_thread
    start_time = time.time()

    try:
        print("[SAP STOCK] Background fetch thread started.")
        logger.info("SAP stock background fetch thread started")

        raw     = _fetch_all_raw_records()
        result  = _filter_and_aggregate(raw)

        elapsed = round(time.time() - start_time, 1)

        with _lock:
            _cache["data"]        = result
            _cache["fetched_at"]  = time.monotonic()
            _cache["error"]       = None
            _cache["_live_count"] = len(raw)   # final count

        print(
            f"[SAP STOCK] Background fetch SUCCESS: "
            f"{len(raw)} raw records → {len(result)} materials cached "
            f"in {elapsed}s"
        )
        logger.info(
            "SAP stock fetch complete: %d raw records → %d materials in %.1fs",
            len(raw), len(result), elapsed
        )

    except Exception as exc:  # noqa: BLE001
        elapsed = round(time.time() - start_time, 1)
        err_msg = str(exc)
        with _lock:
            _cache["error"] = err_msg
        print(f"[SAP STOCK] Background fetch FAILED after {elapsed}s: {err_msg}")
        logger.error("SAP stock background fetch failed after %.1fs: %s", elapsed, exc)

    finally:
        with _lock:
            _fetch_thread           = None
            _cache["_fetching"]     = False
        print("[SAP STOCK] Background fetch thread exiting — is_fetching reset to False.")
        logger.info("SAP stock background fetch thread exiting")


def _trigger_background_fetch():
    """
    Start a new background fetch thread if one isn't already running.

    The _fetching flag is set synchronously under the lock BEFORE t.start()
    so that is_fetching() returns True immediately on the first status poll,
    even if the OS hasn't scheduled the new thread yet.
    """
    global _fetch_thread
    with _lock:
        # If a thread is already running, don't start another one.
        if _fetch_thread is not None and _fetch_thread.is_alive():
            print("[SAP STOCK] Background fetch already running — ignoring duplicate trigger.")
            logger.debug("SAP stock background fetch already running — skipping duplicate trigger")
            return

        t = threading.Thread(
            target=_do_fetch_background,
            daemon=True,
            name="sap-stock-fetch",
        )
        _fetch_thread          = t
        _cache["_fetching"]    = True
        _cache["_live_count"]  = 0   # reset live counter for new run

    t.start()
    print("[SAP STOCK] Background fetch thread launched (is_fetching=True set synchronously).")
    logger.info("SAP stock background fetch thread launched")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_stock(force_refresh: bool = False) -> List[dict]:
    """
    Return currently-cached SAP stock data immediately.

    Triggers a background refresh when:
      - force_refresh=True
      - cache is empty (first call)
      - cache is older than _CACHE_TTL seconds

    NEVER blocks on the SAP HTTP call — always returns from the cache.
    On the very first call (cache empty, fetch not yet done) this returns [];
    the caller should poll is_fetching() + check len(fetch_stock()) > 0.
    """
    now         = time.monotonic()
    cache_empty = _cache["data"] is None
    cache_stale = (not cache_empty) and ((now - _cache["fetched_at"]) >= _CACHE_TTL)

    if force_refresh or cache_empty or cache_stale:
        reason = "force_refresh" if force_refresh else ("cache_empty" if cache_empty else "cache_stale")
        print(f"[SAP STOCK] fetch_stock: triggering background refresh (reason={reason})")
        logger.info("SAP stock fetch_stock: triggering background refresh (reason=%s)", reason)
        _trigger_background_fetch()
    else:
        age = round(now - _cache["fetched_at"])
        print(f"[SAP STOCK] fetch_stock: returning cached data ({len(_cache['data'])} materials, age={age}s)")
        logger.debug(
            "SAP stock: returning cached data (%d materials, age=%ds)",
            len(_cache["data"]), age
        )

    return _cache["data"] or []


def is_fetching() -> bool:
    """
    True while a background SAP fetch is in progress.
    Uses the explicit boolean flag (set synchronously before thread start)
    as the primary signal; thread.is_alive() is a safety fallback.
    """
    with _lock:
        flag         = _cache.get("_fetching", False)
        thread_alive = _fetch_thread is not None and _fetch_thread.is_alive()
    return flag or thread_alive


def cache_age_seconds() -> float:
    """Seconds since the cache was last successfully populated. 0 if never."""
    if _cache["fetched_at"] == 0.0:
        return 0.0
    return time.monotonic() - _cache["fetched_at"]


def last_error() -> str:
    """Last fetch error string, or empty string if none."""
    return _cache["error"] or ""


def live_record_count() -> int:
    """
    Number of raw SAP records fetched so far in the current (or last) run.
    Updates every page during an active fetch, so the status endpoint can
    show 'X records fetched so far' to the user.
    """
    with _lock:
        return _cache.get("_live_count", 0)


def start_background_fetch_on_startup():
    """
    Called once from app/__init__.py when the Flask app is created.
    Triggers the initial background fetch so data is warming up from
    the moment the admin portal opens, before anyone clicks anything.
    """
    print("[SAP STOCK] App startup: triggering initial background fetch.")
    logger.info("SAP stock: startup fetch triggered")
    _trigger_background_fetch()
