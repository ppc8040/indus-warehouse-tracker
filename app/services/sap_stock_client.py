"""
SAP Material Stock API client — API_MATERIAL_STOCK_SRV (OData v2).

Fetches unrestricted warehouse stock (MatlWrhsStkQtyInMatlBaseUnit) for all
plants.  NO $filter is sent to SAP — this matches the reference BOM script
approach and prevents SAP from silently truncating or rejecting the request
when a filter is combined with large result sets.

Only 3 fields are selected from SAP (Material, Plant, MatlWrhsStkQtyInMatlBaseUnit)
— the rest (StorageLocation, Batch, InventorySpecialStockType, InventoryStockType,
MaterialBaseUnit) are not needed and excluding them reduces payload size and
speeds up each page fetch.

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

"""
SAP Material Stock API client — API_MATERIAL_STOCK_SRV (OData v2).

ENHANCED VERSION WITH:
  - Checkpoint-based resumption on timeout
  - Server-side plant filter to reduce data
  - Per-page progress logging
  - Sanity checks on total row counts
  - Increased timeout tolerance
  - Graceful handling of stalled pagination
"""

import os
import sys
import json
import logging
import threading
import time
from typing import Dict
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BASE_URL  = "https://my409486-api.s4hana.cloud.sap/sap/opu/odata/sap/API_MATERIAL_STOCK_SRV"
_USERNAME  = "POWERBI_INTEGRATION"
_PASSWORD  = "d>xEX$dgfFjjw2=2<kbNkH-gPyu-+XnVJF%xJr)v"

# ENHANCED: Increased timeout from 180s to 300s per page (5 minutes)
# With 400+ pages, worst-case total = 33 min (acceptable for background thread)
_TIMEOUT   = 300

# Page size: 500 rows per request (balanced between SAP limit and network efficiency)
_PAGE_SIZE = 1000

# Expected minimum row count for sanity check (you have 200K+ lines)
# If final count is less than this, log a warning
_SANITY_CHECK_MIN_ROWS = 100000

# Checkpoint file location for resumption on timeout
_CHECKPOINT_FILE = '/tmp/sap_stock_checkpoint.json'

SAP_STOCK_CACHE_TTL_SECONDS = 300  # 5 minutes — re-fetch after cache expires

# Plants to keep after fetch (all others discarded in Python)
_VALID_PLANTS = frozenset({"1000", "2000"})

# Material code first-letter whitelist — only Indus part codes
_INDUS_PREFIXES = frozenset("ACI")

# Only these 3 fields are needed — slimmer payload, faster pages
_SELECT_FIELDS = "Material,Plant,MatlWrhsStkQtyInMatlBaseUnit"

# ---------------------------------------------------------------------------
# In-memory cache + background-fetch state
# ---------------------------------------------------------------------------

_cache: Dict = {
    "data":       None,   # list[dict] — the aggregated rows
    "fetched_at": 0.0,    # monotonic time when cache was last populated
    "error":      None,   # last error string, or None
    "row_count":  0,      # total rows fetched in last successful cycle
    "_fetching":  False,  # explicit flag — set synchronously before thread starts,
                          # cleared in finally block; avoids thread.is_alive() race window
}

_fetch_lock   = threading.Lock()
_fetch_thread = None  # currently-running background thread (or None)


class SAPStockError(Exception):
    """Raised when the SAP stock fetch fails unrecoverably."""


# ---------------------------------------------------------------------------
# Checkpoint management (for resumption on timeout)
# ---------------------------------------------------------------------------

def _save_checkpoint(skip: int, fetched_count: int) -> None:
    """Save checkpoint after each successful page fetch."""
    try:
        ckpt = {
            "skip": skip,
            "fetched_count": fetched_count,
            "timestamp": time.time(),
        }
        with open(_CHECKPOINT_FILE, 'w') as f:
            json.dump(ckpt, f)
        logger.debug(f"Checkpoint saved: skip={skip}, fetched={fetched_count}")
    except Exception as exc:
        logger.warning(f"Failed to save checkpoint: {exc}")


def _load_checkpoint() -> dict:
    """Load checkpoint if it exists and is recent (< 1 hour old)."""
    try:
        if os.path.exists(_CHECKPOINT_FILE):
            with open(_CHECKPOINT_FILE, 'r') as f:
                ckpt = json.load(f)
            age = time.time() - ckpt.get('timestamp', 0)
            if age < 3600:  # 1 hour old is still valid
                logger.info(
                    f"Checkpoint loaded: skip={ckpt.get('skip')}, "
                    f"fetched={ckpt.get('fetched_count')}, age={int(age)}s"
                )
                return ckpt
            else:
                logger.info(f"Checkpoint too old ({int(age)}s), ignoring.")
                _delete_checkpoint()
    except Exception as exc:
        logger.warning(f"Failed to load checkpoint: {exc}")
    return {}


def _delete_checkpoint() -> None:
    """Clean up checkpoint file on successful fetch."""
    try:
        if os.path.exists(_CHECKPOINT_FILE):
            os.remove(_CHECKPOINT_FILE)
            logger.debug("Checkpoint deleted (fetch completed successfully)")
    except Exception as exc:
        logger.warning(f"Failed to delete checkpoint: {exc}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _auth() -> HTTPBasicAuth:
    return HTTPBasicAuth(_USERNAME, _PASSWORD)


# ---------------------------------------------------------------------------
# ENHANCED: Fetch with checkpoint resumption
# ---------------------------------------------------------------------------

def _fetch_all_pages() -> list:
    """
    Page through A_MatlStkInAcctMod using $skip/$top with server-side filtering.
    
    ENHANCEMENTS:
      1. Loads checkpoint to resume from last successful page on timeout
      2. Uses server-side plant filter to reduce data volume
      3. Logs per-page progress with cumulative row count
      4. Detects stalled pagination (pages returning 0 rows)
      5. Saves checkpoint after each page for resumption
    
    Returns: list of dicts with Material, Plant, MatlWrhsStkQtyInMatlBaseUnit
    Raises: SAPStockError if fetch fails unrecoverably
    """
    endpoint = f"{_BASE_URL}/A_MatlStkInAcctMod"
    headers  = {"Accept": "application/json"}
    auth     = _auth()

    # Load checkpoint to resume from last successful page
    ckpt = _load_checkpoint()
    skip = ckpt.get('skip', 0)
    all_records = []

    page_attempt = 0
    consecutive_empty_pages = 0

    while True:
        page_num = (skip // _PAGE_SIZE) + 1
        page_attempt += 1

        # ENHANCED: Build params with server-side plant filter
        # This tells SAP to filter BEFORE returning data, reducing network load
        params = {
            "$format": "json",
            "$select": _SELECT_FIELDS,
            "$top": str(_PAGE_SIZE),
            "$skip": str(skip),
            # ENHANCED: Push plant filter to SAP (server-side)
            # Reduces payload and prevents implicit SAP truncation
            "$filter": "Plant eq '1000' or Plant eq '2000'",
        }

        logger.info(
            f"SAP stock fetch: page {page_num} "
            f"(skip={skip}, top={_PAGE_SIZE}, attempt={page_attempt}) — "
            f"cumulative rows so far: {len(all_records)}"
        )

        try:
            resp = requests.get(
                endpoint, params=params,
                headers=headers, auth=auth, timeout=_TIMEOUT
            )
            resp.raise_for_status()

        except requests.Timeout:
            logger.error(
                f"SAP stock fetch: TIMEOUT on page {page_num} (skip={skip}). "
                f"Fetched {len(all_records)} rows so far. "
                f"Checkpoint saved — will resume on next cycle."
            )
            _save_checkpoint(skip, len(all_records))
            raise SAPStockError(
                f"Timeout on page {page_num} (skip={skip}) after {len(all_records)} rows. "
                f"Checkpoint saved for resumption."
            ) from None

        except requests.RequestException as exc:
            logger.error(
                f"SAP stock fetch: HTTP error on page {page_num} (skip={skip}): {exc}"
            )
            raise SAPStockError(
                f"HTTP error on page {page_num} (skip={skip}): {exc}"
            ) from exc

        try:
            body = resp.json()
        except ValueError as exc:
            logger.error(
                f"SAP stock fetch: Invalid JSON on page {page_num} (skip={skip}): {exc}"
            )
            raise SAPStockError(
                f"Invalid JSON on page {page_num} (skip={skip}): {exc}"
            ) from exc

        results = body.get("d", {}).get("results", [])
        if not isinstance(results, list):
            raise SAPStockError(
                f"Unexpected response structure on page {page_num}: "
                f"'d.results' is not a list"
            )

        rows_this_page = len(results)
        all_records.extend(results)
        consecutive_empty_pages = 0 if rows_this_page > 0 else consecutive_empty_pages + 1

        logger.info(
            f"SAP stock fetch: page {page_num} → {rows_this_page} records "
            f"(total cumulative: {len(all_records)})"
        )

        # ENHANCED: Detect stalled pagination (consecutive empty pages)
        if consecutive_empty_pages > 2:
            logger.warning(
                f"SAP stock fetch: 3+ consecutive empty pages detected at skip={skip}. "
                f"Pagination may be stalled. Stopping fetch with {len(all_records)} rows."
            )
            break

        # Normal termination: received fewer rows than requested (last page)
        if rows_this_page < _PAGE_SIZE:
            logger.info(
                f"SAP stock fetch: Last page detected "
                f"(page {page_num} returned {rows_this_page} rows, < {_PAGE_SIZE}). "
                f"Total fetch: {len(all_records)} rows across {page_num} pages."
            )
            break

        # Save checkpoint after each successful page for resumption
        _save_checkpoint(skip + rows_this_page, len(all_records))

        skip += _PAGE_SIZE

    # ENHANCED: Sanity check — warn if row count suspiciously low
    if len(all_records) < _SANITY_CHECK_MIN_ROWS:
        logger.warning(
            f"⚠️  SAP stock fetch returned only {len(all_records)} rows. "
            f"Expected minimum ~{_SANITY_CHECK_MIN_ROWS} (you have 200K+ stock lines). "
            f"SAP may have silently truncated results. Verify SAP logs and re-run."
        )

    # On successful completion, clean up checkpoint
    _delete_checkpoint()

    return all_records


# ---------------------------------------------------------------------------
# Aggregation (unchanged from original)
# ---------------------------------------------------------------------------

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

        # Plant filter: only 1000 and 2000 (now redundant with server-side filter, but kept for safety)
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
        f"SAP stock aggregate: {len(records)} raw records "
        f"→ {len(result)} Indus materials (Plants 1000+2000)"
    )
    return result


# ---------------------------------------------------------------------------
# Background fetch worker
# ---------------------------------------------------------------------------

def _do_fetch_background():
    """
    Worker function running in a daemon thread.
    Fetches all pages, aggregates, and updates the in-memory cache.
    Errors are stored in cache['error'] but never propagated.

    NOTE: _cache["_fetching"] is set to True BEFORE this thread is started
    (inside _trigger_background_fetch, under the lock) so that is_fetching()
    returns True immediately — even before the OS has marked the thread alive.
    This flag is the authoritative source for is_fetching(); thread.is_alive()
    is only used as a fallback safety check.
    """
    global _fetch_thread
    try:
        logger.info(
            "SAP stock background fetch started "
            "(server-side plant filter, full pagination with checkpoint resumption)"
        )
        raw    = _fetch_all_pages()
        result = _aggregate(raw)
        _cache["data"]       = result
        _cache["fetched_at"] = time.monotonic()
        _cache["error"]      = None
        _cache["row_count"]  = len(raw)
        logger.info(
            f"SAP stock background fetch complete: "
            f"{len(raw)} raw records → {len(result)} materials cached"
        )
    except Exception as exc:  # noqa: BLE001
        _cache["error"] = str(exc)
        logger.error(f"SAP stock background fetch failed: {exc}")
    finally:
        # Clear both the thread reference and the explicit flag under the lock
        with _fetch_lock:
            _fetch_thread = None
            _cache["_fetching"] = False
        logger.info("SAP stock background fetch thread exiting — is_fetching reset to False")


def _trigger_background_fetch():
    """
    Start a background fetch thread if one is not already running.

    KEY FIX: _cache["_fetching"] is set to True synchronously here, inside
    the lock, BEFORE t.start() is called.  This guarantees that any call to
    is_fetching() — even one that arrives microseconds after this function
    returns — will see True without waiting for the OS to schedule the thread.
    """
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
        # Set the explicit flag BEFORE starting the thread so that
        # is_fetching() returns True immediately on the very first poll.
        _cache["_fetching"] = True
        t.start()
        logger.info("SAP stock background fetch thread started (is_fetching=True set synchronously)")


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
            f"SAP stock: returning cached data "
            f"({len(_cache['data'] or [])} rows, age {now - _cache['fetched_at']:.0f}s)"
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
    """
    Return True if a background SAP fetch is currently in progress.

    Uses the explicit _cache["_fetching"] boolean as the primary signal —
    this is set synchronously (under the lock) before the thread is even
    started, so there is zero race window between the kick-off request and
    the first frontend poll.  thread.is_alive() is kept as a safety net in
    case the thread dies without reaching the finally block.
    """
    with _fetch_lock:
        explicit_flag = _cache.get("_fetching", False)
        thread_alive  = _fetch_thread is not None and _fetch_thread.is_alive()
        return explicit_flag or thread_alive


def get_cache_stats() -> dict:
    """Return diagnostic info about the cache state."""
    return {
        "cached_materials": len(_cache["data"] or []),
        "raw_rows_fetched": _cache["row_count"],
        "cache_age_seconds": cache_age_seconds(),
        "is_fetching": is_fetching(),
        "last_error": last_error(),
        "checkpoint_exists": os.path.exists(_CHECKPOINT_FILE),
    }
