"""
movement_service.py — Core business logic, backed by PostgreSQL.

All data operations now go through db_client.py.
graph_client.py is dormant and no longer imported here.

Movement rules (unchanged from Excel era):

  Duplicate blocking:
    A movement is a duplicate when the most-recent row for this HU in
    `movements` has the SAME movement_type as the incoming request.
    An IN followed by an OUT (or vice versa) is a valid cycle — not a dup.
    Overrides bypass transition checks but NEVER bypass duplicate checks.

  Transition rules (skipped for supervisor overrides):
    STOCK_IN:
      Blocked if the same HU + Indus_Part + Qty is already IN.
      Different part/qty on the same HU = new pallet run → allowed.
    STOCK_OUT:
      Blocked if already OUT, or if the HU has never been seen.
      Outgoing part/qty must match stored values (wrong-label guard).
      Location lock: outgoing location must match pallet's last_location.

  Location rules:
    Every STOCK_IN and STOCK_OUT requires a `location` (the physical
    warehouse zone being operated on — M4, M5, M6, M4_STORE, M16_STORE).

  Destination (new — STOCK_OUT only):
    STOCK_OUT additionally accepts `from_location` and `to_location`.
      from_location — auto-derived from pallet_status.last_location;
                      the caller may pass it explicitly or leave it empty
                      (the service fills it from pallet_status).
      to_location   — where the pallet is going (Customer, Port Jebel Ali,
                      Zone M2, Quarantine, Production, …).
    Both are stored in the movements row. Neither field affects existing
    validation logic — they are purely informational/audit fields.

  Error logging:
    _log_error() writes to the `movements` table with movement_type
    'ERROR' as a structured audit event rather than a separate table,
    keeping the schema simple. Failures in _log_error itself are swallowed
    and only logged to the Python logger.

Result object:
    Every public function returns a Result instance with:
      .success  bool
      .status   str  (e.g. 'success_synced', 'rejected_duplicate')
      .message  str  (human-readable, shown to worker)
      .data     dict (extra fields for the frontend)
"""

import logging
import uuid
from datetime import datetime, timezone

from app.utils.validators import (
    validate_handling_unit,
    validate_movement_type,
    validate_indus_part,
    validate_customer_part,
    validate_pallet_qty,
)
from app.services.db_client import (
    DbError,
    insert_movement,
    get_last_movement_for_hu,
    get_recent_movements,
    get_all_movements_for_hu,
    get_pallet_status,
    upsert_pallet_status,
    get_all_pallet_statuses,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_LOCATIONS = {'M4', 'M5', 'M6', 'M4_STORE', 'M16_STORE'}

# to_location options surfaced to the frontend via /api/destinations
VALID_DESTINATIONS = [
    'Customer',
    'Port Jebel Ali',
    'Port Khalifa',
    'Zone M2',
    'Zone M4',
    'Zone M5',
    'Zone M6',
    'M4_STORE',
    'M16_STORE',
    'Quarantine',
    'Production',
    'Return to Supplier',
    'Other',
]


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

class Result:
    """Lightweight result carrier returned by every public function."""

    def __init__(self, success: bool, status: str, message: str, data: dict = None):
        self.success = success
        self.status = status
        self.message = message
        self.data = data or {}

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "status": self.status,
            "message": self.message,
            "data": self.data,
        }


# ---------------------------------------------------------------------------
# Public API — process_movement
# ---------------------------------------------------------------------------

def process_movement(
    handling_unit: str,
    movement_type: str,
    indus_part: str = "",
    customer_part: str = "",
    pallet_qty: str = "",
    job_no: str = "",
    operator_id: str = "WORKER",
    device_id: str = "",
    is_override: bool = False,
    override_reason: str = "",
    client_timestamp: str = "",
    idempotency_key: str = "",
    location: str = "",
    to_location: str = "",
) -> Result:
    """
    Validate and record a STOCK_IN or STOCK_OUT movement.

    Parameters
    ----------
    handling_unit     : HU barcode (18–20 digits)
    movement_type     : 'STOCK_IN' or 'STOCK_OUT'
    indus_part        : Indus internal part code (e.g. CEJ1773)
    customer_part     : Customer part code (optional)
    pallet_qty        : Quantity on the pallet (positive integer string)
    job_no            : Job / work-order reference (optional)
    operator_id       : Worker identifier (default 'WORKER')
    device_id         : Device name / ID (optional)
    is_override       : True when a supervisor overrides a blocked scan
    override_reason   : Free-text reason for the override (optional)
    client_timestamp  : ISO timestamp from the device (falls back to server time)
    idempotency_key   : Client-generated key for offline dedup (logged only)
    location          : Physical zone being operated on (M4, M5, M6, …)
    to_location       : Destination for STOCK_OUT (Customer, Port JXB, …)
    """

    # ── 1. Validate inputs ────────────────────────────────────────────────
    ok_hu, hu = validate_handling_unit(handling_unit)
    if not ok_hu:
        _log_error('VALIDATION_FAIL', handling_unit, hu)
        return Result(False, 'rejected_validation', hu)

    ok_mt, movement = validate_movement_type(movement_type)
    if not ok_mt:
        _log_error('TYPE_INVALID', hu, movement)
        return Result(False, 'rejected_validation', movement)

    ok_indus, indus_clean = validate_indus_part(indus_part)
    if not ok_indus:
        _log_error('INVALID_INDUS_PART', hu, indus_clean)
        return Result(False, 'rejected_validation', indus_clean)

    ok_cust, cust_clean = validate_customer_part(customer_part, indus_clean)
    if not ok_cust:
        _log_error('INVALID_CUSTOMER_PART', hu, cust_clean)
        return Result(False, 'rejected_validation', cust_clean)

    ok_qty, qty_clean = validate_pallet_qty(pallet_qty)
    if not ok_qty:
        _log_error('INVALID_QTY', hu, qty_clean)
        return Result(False, 'rejected_validation', qty_clean)

    loc = _s(location).strip().upper()
    if loc not in VALID_LOCATIONS:
        msg = ('Location is required. Select a valid location '
               '(M4, M5, M6, M4_STORE, M16_STORE).')
        _log_error('MISSING_LOCATION', hu, f'Invalid or missing location: {location!r}')
        return Result(False, 'rejected_validation', msg)

    # ── 2. Build event metadata ───────────────────────────────────────────
    now_utc = datetime.now(timezone.utc)
    timestamp = client_timestamp or now_utc.isoformat()
    event_id = str(uuid.uuid4())[:12].upper()

    # ── 3. Duplicate check (always — overrides included) ──────────────────
    dup_err = _check_duplicate(hu, movement)
    if dup_err:
        _log_error('DUPLICATE', hu, dup_err)
        return Result(False, 'rejected_duplicate', dup_err,
                      {'attempted': movement})

    # ── 4. Fetch current pallet state ─────────────────────────────────────
    try:
        current = get_pallet_status(hu)   # dict or None
    except DbError as exc:
        logger.error("get_pallet_status failed for %s: %s", hu, exc)
        return Result(False, 'failed_write_logged',
                      'System temporarily unavailable. Please try again.',
                      {'needs_sync': True})

    # ── 5. Transition check (skipped for supervisor overrides) ────────────
    if not is_override:
        cur_state = _s(current.get('last_movement_type', 'NONE')).upper() \
            if current else 'NONE'
        err = _check_transition(
            hu=hu,
            movement=movement,
            cur_state=cur_state,
            indus_part=indus_clean,
            pallet_qty=qty_clean,
            current_row=current,
            incoming_location=loc,
        )
        if err:
            _log_error('INVALID_TRANSITION', hu,
                       f"{movement} blocked. state={cur_state}")
            return Result(False, 'rejected_transition', err,
                          {'current_state': cur_state, 'attempted': movement})

    # ── 6. Resolve from_location ──────────────────────────────────────────
    # For STOCK_OUT: from_location is the zone the pallet is currently at.
    # We derive it from pallet_status so the frontend doesn't need to send it.
    # An explicit value from the caller is accepted but overridden by the
    # stored value when the pallet is currently IN the warehouse.
    from_loc = ""
    if movement == 'STOCK_OUT' and current:
        from_loc = _s(current.get('last_location', '')).strip().upper() or loc
    elif movement == 'STOCK_OUT':
        from_loc = loc  # fallback: use the scanned location

    to_loc = _s(to_location).strip() if movement == 'STOCK_OUT' else ""

    # ── 7. Determine validation status ───────────────────────────────────
    validation_status = "OVERRIDE" if is_override else "VALID"

    # ── 8. Write to PostgreSQL ────────────────────────────────────────────
    try:
        insert_movement(
            event_id=event_id,
            timestamp_utc=timestamp,
            handling_unit=hu,
            movement_type=movement,
            indus_part=indus_clean,
            customer_part=cust_clean,
            pallet_qty=qty_clean,
            job_no=_s(job_no).strip(),
            operator_id=operator_id or 'WORKER',
            device_id=device_id or '',
            sync_status='SYNCED',
            validation_status=validation_status,
            location=loc,
            from_location=from_loc,
            to_location=to_loc,
        )
    except DbError as exc:
        logger.error("insert_movement failed: %s", exc)
        return Result(False, 'failed_write_logged',
                      'Could not save. Saved locally — will sync automatically.',
                      {'needs_sync': True})

    # ── 9. Upsert pallet_status ───────────────────────────────────────────
    current_status = 'IN_WAREHOUSE' if movement == 'STOCK_IN' else 'OUT_OF_WAREHOUSE'
    try:
        upsert_pallet_status(
            handling_unit=hu,
            current_status=current_status,
            event_id=event_id,
            last_movement_type=movement,
            last_movement_time=timestamp,
            indus_part=indus_clean,
            pallet_qty=qty_clean,
            last_location=loc,
        )
    except DbError as exc:
        # Movement was written — log the status-update failure but don't
        # surface it as an error to the worker. Status will self-heal on
        # the next movement for this HU.
        logger.error("upsert_pallet_status failed (movement already written): %s", exc)

    logger.info(
        "Movement OK: %s HU=%s part=%s location=%s from=%s to=%s event=%s",
        movement, hu, indus_clean, loc, from_loc, to_loc, event_id,
    )

    display_type = 'Stock IN' if movement == 'STOCK_IN' else 'Stock OUT'
    return Result(
        True,
        'success_synced',
        f"{display_type} recorded.",
        {
            'handling_unit': hu,
            'movement_type': movement,
            'event_id': event_id,
            'timestamp': timestamp,
            'current_status': current_status,
            'location': loc,
            'from_location': from_loc,
            'to_location': to_loc,
            'indus_part': indus_clean,
            'customer_part': cust_clean,
            'pallet_qty': qty_clean,
            'job_no': _s(job_no),
            'is_override': is_override,
        },
    )


# ---------------------------------------------------------------------------
# Public API — offline sync
# ---------------------------------------------------------------------------

def sync_offline_event(event: dict) -> Result:
    """Re-process a single offline-queued event dict."""
    return process_movement(
        handling_unit=event.get('handling_unit', ''),
        movement_type=event.get('movement_type', ''),
        indus_part=event.get('indus_part', ''),
        customer_part=event.get('customer_part', ''),
        pallet_qty=event.get('pallet_qty', ''),
        job_no=event.get('job_no', ''),
        operator_id=event.get('operator_id', 'OFFLINE_WORKER'),
        device_id=event.get('device_id', ''),
        is_override=event.get('is_override', False),
        override_reason=event.get('override_reason', ''),
        client_timestamp=event.get('timestamp', ''),
        idempotency_key=event.get('idempotency_key', ''),
        location=event.get('location', ''),
        to_location=event.get('to_location', ''),
    )


# ---------------------------------------------------------------------------
# Public API — admin / query helpers
# ---------------------------------------------------------------------------

def get_recent_movements_service(limit: int = 50) -> list:
    """Return the most recent `limit` movement rows (dicts), newest first."""
    try:
        return get_recent_movements(limit)
    except DbError as exc:
        logger.error("get_recent_movements failed: %s", exc)
        return []


def get_all_pallet_statuses_service() -> list:
    """Return all pallet_status rows as a list of dicts."""
    try:
        return get_all_pallet_statuses()
    except DbError as exc:
        logger.error("get_all_pallet_statuses failed: %s", exc)
        return []


def get_pallet_history(handling_unit: str) -> list:
    """Return full movement history for a single HU, oldest first."""
    try:
        return get_all_movements_for_hu(handling_unit)
    except DbError as exc:
        logger.error("get_all_movements_for_hu failed for %s: %s", handling_unit, exc)
        return []


def get_destinations() -> list:
    """Return the list of valid to_location values for the frontend picker."""
    return VALID_DESTINATIONS


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _s(val) -> str:
    """Safely coerce any value to str, returning '' for None."""
    return '' if val is None else str(val)


def _normalise(val) -> str:
    """Strip whitespace, uppercase, remove trailing '.0' from numeric strings."""
    s = _s(val).strip()
    if s.startswith("'"):
        s = s[1:]
    s = s.upper()
    if s.endswith('.0') and s[:-2].isdigit():
        s = s[:-2]
    return s


def _check_duplicate(hu: str, movement: str) -> str | None:
    """
    Query `movements` for the most recent row matching this HU.

    Returns an error message string if it's a duplicate, None if clean.

    Duplicate = same HU, same movement_type as the most-recent record.
    Opposite direction (IN→OUT or OUT→IN) is a valid cycle — allowed.
    Brand new HU (no rows) is always allowed.

    Fails closed: on DbError, returns a retry message so no movement
    silently bypasses the check.
    """
    try:
        last = get_last_movement_for_hu(hu)
    except DbError as exc:
        logger.warning("Duplicate check DB error (blocking movement): %s", exc)
        return 'System is temporarily busy. Please try again in a moment.'

    if not last:
        return None  # brand-new HU

    last_move = _normalise(last.get('movement_type', ''))
    incoming  = _normalise(movement)

    if last_move == incoming:
        direction = 'Stock IN' if incoming == 'STOCK_IN' else 'Stock OUT'
        return (
            f"HU {hu} was already recorded as {direction}. "
            f"Scan a different pallet, or use supervisor override "
            f"if this is intentional."
        )

    # Opposite direction — legitimate cycle
    return None


def _check_transition(
    hu: str,
    movement: str,
    cur_state: str,
    indus_part: str = '',
    pallet_qty: str = '',
    current_row: dict = None,
    incoming_location: str = '',
) -> str | None:
    """
    Validate the movement transition using pallet_status data.
    Only called when is_override=False.

    Returns an error message string if blocked, None if allowed.
    """
    if movement == 'STOCK_IN' and cur_state == 'STOCK_IN':
        if current_row:
            stored_part = _normalise(current_row.get('indus_part', ''))
            stored_qty  = _normalise(current_row.get('pallet_qty', ''))
            if (stored_part == _normalise(indus_part) and
                    stored_qty == _normalise(pallet_qty)):
                return (
                    f"Pallet {hu} ({indus_part}, qty {pallet_qty}) is already "
                    f"IN the warehouse. Do Stock OUT first, or use supervisor override."
                )
            # Different part/qty on same HU = new run — allow
            return None
        return (
            f"Pallet {hu} is already IN the warehouse. "
            f"Do Stock OUT first, or use supervisor override."
        )

    if movement == 'STOCK_OUT':
        if cur_state == 'STOCK_OUT':
            return (f"Pallet {hu} is already OUT of the warehouse. "
                    f"Use supervisor override if needed.")

        if cur_state in ('NONE', '', 'NOT IN SYSTEM', 'UNKNOWN'):
            return (f"Pallet {hu} was never stocked IN. "
                    f"Do Stock IN first, or use supervisor override.")

        # Pallet is currently IN — validate outgoing scan matches stored identity
        if current_row:
            stored_part = _normalise(current_row.get('indus_part', ''))
            stored_qty  = _normalise(current_row.get('pallet_qty', ''))
            incoming_part = _normalise(indus_part)
            incoming_qty  = _normalise(pallet_qty)

            if stored_part and stored_part != incoming_part:
                return (
                    f"Pallet {hu} is recorded as "
                    f"{current_row.get('indus_part')} "
                    f"(qty {current_row.get('pallet_qty')}) but the scanned "
                    f"label shows {indus_part} (qty {pallet_qty}). "
                    f"Check the label, or use supervisor override."
                )
            if stored_qty and stored_qty != incoming_qty:
                return (
                    f"Pallet {hu} was stocked IN with qty "
                    f"{current_row.get('pallet_qty')} but the scanned label "
                    f"shows qty {pallet_qty}. Check the label, or use supervisor override."
                )

            stored_location = _normalise(current_row.get('last_location', ''))
            if stored_location and stored_location != _normalise(incoming_location):
                return (
                    f"Pallet {hu} was stocked IN at {stored_location}. "
                    f"You must do Stock OUT from {stored_location}, "
                    f"not {incoming_location}. "
                    f"Use supervisor override if this is intentional."
                )

    return None


def _log_error(error_code: str, handling_unit: str, message: str) -> None:
    """
    Best-effort structured error log.
    Writes a row to `movements` with movement_type='ERROR' so all events
    stay in one table and the admin panel can surface failures easily.
    Never raises — swallows any DbError and logs to Python logger instead.
    """
    try:
        insert_movement(
            event_id=str(uuid.uuid4())[:12].upper(),
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            handling_unit=_s(handling_unit) or 'UNKNOWN',
            movement_type='ERROR',
            operator_id='SYSTEM',
            sync_status='SYNCED',
            validation_status=error_code,
            location='',
            job_no=str(message)[:500],
        )
    except Exception as exc:
        logger.error(
            "_log_error write failed: %s. Original: code=%s hu=%s msg=%s",
            exc, error_code, handling_unit, message,
        )
