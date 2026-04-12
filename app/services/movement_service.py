"""
Core business logic — scan-to-log architecture.

NO Master_Parts lookup. All pallet data comes from scanning the label:
  - QR code 1: Indus_Part (e.g. CCR2324)
  - QR code 2: Customer_Part (e.g. ENG-1440)
  - QR code 3: Pallet_Qty (e.g. 39)
  - Barcode (Code 128): Handling_Unit (e.g. 100000000000839157)

The worker scans all 4 codes, the app assembles the record, then logs it.

Pallet_Status still tracks current state for transition validation.
Movement_Log is append-only.
Error_Log captures all failures.

Movement_Log (13 cols A-M):
  A=Event_ID  B=Timestamp_UTC  C=Handling_Unit  D=Indus_Part
  E=Customer_Part  F=Pallet_Qty  G=Job_No  H=Movement_Type
  I=Operator_ID  J=Device_ID  K=Sync_Status  L=Validation_Status
  M=Location

Pallet_Status (8 cols A-H):
  A=Handling_Unit  B=Current_Status  C=Last_Event_ID
  D=Last_Movement_Type  E=Last_Movement_Time  F=Indus_Part
  G=Pallet_Qty  H=Last_Location

Error_Log (7 cols A-G):
  A=Error_ID  B=Timestamp_UTC  C=Handling_Unit  D=Stage
  E=Error_Code  F=Error_Message  G=Resolved_Flag

Duplicate detection strategy:
  Instead of in-memory idempotency keys (which are lost on dyno restart,
  don't work across multiple dynos, and break on offline replay with
  different timestamps), duplicate blocking is done by querying Movement_Log
  directly. A scan is rejected as a duplicate when a row already exists with
  the same (Handling_Unit, Indus_Part, Customer_Part, Pallet_Qty,
  Movement_Type) AND Validation_Status != OVERRIDE.
  Supervisor overrides are always allowed through regardless.

Location rules:
  - Every STOCK_IN or STOCK_OUT must include a location (M4, M5, M6,
    M4_STORE, M16_STORE).
  - When a pallet is STOCK_OUT, the selected location must match the
    Last_Location stored in Pallet_Status (i.e. the location it was
    last STOCK_IN at). Override bypasses this check.
  - After any STOCK_OUT, the pallet is free and may be STOCK_IN at
    any location on the next movement.
  - Last_Location in Pallet_Status always reflects the location of
    the most recent movement (IN or OUT).

Handling_Unit apostrophe note:
  In the workbook, HU values are stored with a leading apostrophe (e.g.
  '100000000000839157) so Excel treats them as text and does not corrupt the
  18-digit number as a float. The Graph API returns the raw value without the
  apostrophe. The _normalise_cell helper in graph_client.py strips whitespace
  and uppercases, so all HU comparisons work correctly without any special
  handling here.
"""
import uuid
import logging
from datetime import datetime, timezone

from flask import current_app
from app.services.graph_client import (
    find_row_as_dict,
    append_row,
    upsert_row,
    read_all_rows_as_dicts,
    GraphAPIError,
)
from app.utils.validators import (
    validate_handling_unit,
    validate_movement_type,
    validate_indus_part,
    validate_customer_part,
    validate_pallet_qty,
)

logger = logging.getLogger(__name__)

MOVEMENT_LOG_COLS = 13
PALLET_STATUS_COLS = 8
ERROR_LOG_COLS = 7

VALID_LOCATIONS = {'M4', 'M5', 'M6', 'M4_STORE', 'M16_STORE'}

# Movement_Log column positions (0-based) used for duplicate detection
_ML_COL_HU         = 2   # C: Handling_Unit
_ML_COL_INDUS      = 3   # D: Indus_Part
_ML_COL_CUST       = 4   # E: Customer_Part
_ML_COL_QTY        = 5   # F: Pallet_Qty
_ML_COL_MOVE_TYPE  = 7   # H: Movement_Type
_ML_COL_VALID_STAT = 11  # L: Validation_Status


class Result:
    def __init__(self, success, status, message, data=None):
        self.success = success
        self.status = status
        self.message = message
        self.data = data or {}

    def to_dict(self):
        return {"success": self.success, "status": self.status,
                "message": self.message, "data": self.data}


# ════════════════════════════════════════════════════════════════════════════════
#  PROCESS MOVEMENT — data comes from scanned label, no master lookup
# ════════════════════════════════════════════════════════════════════════════════

def process_movement(
    handling_unit,
    movement_type,
    indus_part="",
    customer_part="",
    pallet_qty="",
    job_no="",
    operator_id="WORKER",
    device_id="",
    is_override=False,
    override_reason="",
    client_timestamp="",
    idempotency_key="",
    location="",
):
    """
    Record a Stock IN / Stock OUT movement.
    All pallet data (indus_part, customer_part, pallet_qty) comes from the scanned label.
    No master data lookup needed.
    location must be one of: M4, M5, M6, M4_STORE, M16_STORE.
    """

    # ── Validate HU and movement type ──
    ok_hu, hu = validate_handling_unit(handling_unit)
    if not ok_hu:
        _log_error('MOVEMENT', 'VALIDATION_FAIL', handling_unit, hu)
        return Result(False, 'rejected_validation', hu)

    ok_mt, movement = validate_movement_type(movement_type)
    if not ok_mt:
        _log_error('MOVEMENT', 'TYPE_INVALID', hu, movement)
        return Result(False, 'rejected_validation', movement)

    # ── Validate Indus Part ──
    ok_indus, indus_clean = validate_indus_part(indus_part)
    if not ok_indus:
        _log_error('MOVEMENT', 'INVALID_INDUS_PART', hu, indus_clean)
        return Result(False, 'rejected_validation', indus_clean)

    # ── Validate Customer Part ──
    ok_cust, cust_clean = validate_customer_part(customer_part, indus_clean)
    if not ok_cust:
        _log_error('MOVEMENT', 'INVALID_CUSTOMER_PART', hu, cust_clean)
        return Result(False, 'rejected_validation', cust_clean)

    # ── Validate Pallet Qty ──
    ok_qty, qty_clean = validate_pallet_qty(pallet_qty)
    if not ok_qty:
        _log_error('MOVEMENT', 'INVALID_QTY', hu, qty_clean)
        return Result(False, 'rejected_validation', qty_clean)

    # ── Validate location ──
    loc = _s(location).strip().upper()
    if loc not in VALID_LOCATIONS:
        _log_error('MOVEMENT', 'MISSING_LOCATION', hu, f'Invalid or missing location: {location}')
        return Result(False, 'rejected_validation',
                      'Location is required. Select a valid location (M4, M5, M6, M4_STORE, M16_STORE).')

    now_utc = datetime.now(timezone.utc)
    timestamp = client_timestamp or now_utc.isoformat()
    event_id = str(uuid.uuid4())[:12].upper()

    try:
        log_sheet    = current_app.config['SHEET_MOVEMENT_LOG']
        status_sheet = current_app.config['SHEET_PALLET_STATUS']

        # ── Duplicate check against Movement_Log (persistent, dyno-safe) ──
        # Fix 3: now fails closed — Excel read errors block the movement with
        # a friendly retry message instead of silently allowing it through.
        if not is_override:
            dup_err = _check_duplicate_in_log(
                log_sheet, hu,
                indus_clean,
                cust_clean,
                qty_clean,
                movement,
            )
            if dup_err:
                _log_error('MOVEMENT', 'DUPLICATE', hu, dup_err)
                return Result(False, 'rejected_validation', dup_err,
                              {'attempted': movement})

        # ── State transition check (from Pallet_Status) ──
        current  = find_row_as_dict(status_sheet, 'Handling_Unit', hu)
        cur_state = _s(current.get('Last_Movement_Type', 'NONE')).upper() if current else 'NONE'

        err = _check_transition(hu, movement, cur_state, is_override,
                                indus_part=indus_clean,
                                pallet_qty=qty_clean,
                                current_row=current,
                                incoming_location=loc)
        if err:
            _log_error('MOVEMENT', 'INVALID_TRANSITION', hu,
                       f"{movement} blocked. state={cur_state} override={is_override}")
            return Result(False, 'rejected_validation', err,
                          {'current_state': cur_state, 'attempted': movement})

        # ── Determine validation status ──
        validation_status = "OVERRIDE" if is_override else "VALID"

        # ── Write Movement_Log (append-only, 13 columns A-M) ──
        log_row = [
            event_id,                           # A: Event_ID
            _fmt_timestamp(timestamp),          # B: Timestamp_UTC
            hu,                                 # C: Handling_Unit
            indus_clean,                        # D: Indus_Part
            cust_clean,                         # E: Customer_Part
            qty_clean,                          # F: Pallet_Qty
            _s(job_no).strip(),                 # G: Job_No
            movement,                           # H: Movement_Type
            operator_id,                        # I: Operator_ID
            device_id,                          # J: Device_ID
            'SYNCED',                           # K: Sync_Status
            validation_status,                  # L: Validation_Status
            loc,                                # M: Location
        ]
        append_row(log_sheet, log_row, MOVEMENT_LOG_COLS)

        # ── Upsert Pallet_Status (8 columns A-H) ──
        current_status = 'IN_WAREHOUSE' if movement == 'STOCK_IN' else 'OUT_OF_WAREHOUSE'
        status_row = [
            hu,                                 # A: Handling_Unit
            current_status,                     # B: Current_Status
            event_id,                           # C: Last_Event_ID
            movement,                           # D: Last_Movement_Type
            _fmt_timestamp(timestamp),          # E: Last_Movement_Time
            indus_clean,                        # F: Indus_Part
            qty_clean,                          # G: Pallet_Qty
            loc,                                # H: Last_Location
        ]
        upsert_row(status_sheet, 0, hu, status_row, PALLET_STATUS_COLS)

        logger.info(f"Movement OK: {movement} HU={hu} part={indus_clean} location={loc} event={event_id}")

        display_type = 'Stock IN' if movement == 'STOCK_IN' else 'Stock OUT'
        return Result(True, 'success_synced', f"{display_type} recorded.",
                      {
                          'handling_unit': hu,
                          'movement_type': movement,
                          'event_id': event_id,
                          'timestamp': timestamp,
                          'current_status': current_status,
                          'location': loc,
                          'indus_part': indus_clean,
                          'customer_part': cust_clean,
                          'pallet_qty': qty_clean,
                          'job_no': _s(job_no),
                          'is_override': is_override,
                      })

    except GraphAPIError as e:
        _log_error('MOVEMENT', 'GRAPH_ERROR', hu, f"Graph write failed: {e.message}")
        return Result(False, 'failed_write_logged',
                      "Could not save. Saved locally — will sync automatically.",
                      {'needs_sync': True})
    except Exception as e:
        logger.exception(f"Unexpected movement error: {e}")
        _log_error('MOVEMENT', 'UNEXPECTED', hu, str(e))
        return Result(False, 'failed_write_logged',
                      "Something went wrong. Saved locally.",
                      {'needs_sync': True})


# ════════════════════════════════════════════════════════════════════════════════
#  OFFLINE SYNC
# ════════════════════════════════════════════════════════════════════════════════

def sync_offline_event(event):
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
    )


# ════════════════════════════════════════════════════════════════════════════════
#  ADMIN QUERIES
# ════════════════════════════════════════════════════════════════════════════════

def get_recent_movements(limit=50):
    try:
        rows = read_all_rows_as_dicts(current_app.config['SHEET_MOVEMENT_LOG'])
        rows.reverse()
        return rows[:limit]
    except Exception as e:
        logger.error(f"Read movement log failed: {e}")
        return []


def get_error_log(limit=100):
    try:
        rows = read_all_rows_as_dicts(current_app.config['SHEET_ERROR_LOG'])
        rows.reverse()
        return rows[:limit]
    except Exception as e:
        logger.error(f"Read error log failed: {e}")
        return []


def get_all_pallet_statuses():
    try:
        return read_all_rows_as_dicts(current_app.config['SHEET_PALLET_STATUS'])
    except Exception as e:
        logger.error(f"Read pallet statuses failed: {e}")
        return []


# ════════════════════════════════════════════════════════════════════════════════
#  PRIVATE HELPERS
# ════════════════════════════════════════════════════════════════════════════════

def _s(val):
    if val is None:
        return ''
    return str(val)


def _normalise_str(val):
    """Strip, uppercase, remove trailing .0 from numeric strings.
    Also strips a leading apostrophe that Excel inserts as a text-prefix
    for long numeric strings (e.g. '100000000000839157 -> 100000000000839157).
    The Graph API typically returns the value without the apostrophe, but
    this guard ensures safety in all paths.
    """
    s = str(val).strip() if val is not None else ''
    # Strip Excel text-prefix apostrophe if present
    if s.startswith("'"):
        s = s[1:]
    s = s.upper()
    # Handle '39.0' → '39' (Excel float leakage)
    if s.endswith('.0') and s[:-2].isdigit():
        s = s[:-2]
    return s


def _check_duplicate_in_log(log_sheet, hu, indus_part, customer_part, pallet_qty, movement):
    """
    Query Movement_Log to see if an identical non-override row already exists.

    Fields checked (all must match):
      C=Handling_Unit, D=Indus_Part, E=Customer_Part,
      F=Pallet_Qty,   H=Movement_Type, L=Validation_Status

    A row is a duplicate only when Validation_Status is NOT 'OVERRIDE'.
    Supervisor overrides are intentional re-submissions and are always
    allowed to pass through this check.

    Fix 3: on Excel read failure, returns a friendly retry message instead
    of None (which previously let all movements through silently).

    Returns an error message string if duplicate found or read failed,
    or None if clean.
    """
    try:
        from app.services.graph_client import read_all_rows
        headers, rows = read_all_rows(log_sheet)

        n_hu   = _normalise_str(hu)
        n_part = _normalise_str(indus_part)
        n_cust = _normalise_str(customer_part)
        n_qty  = _normalise_str(pallet_qty)
        n_move = _normalise_str(movement)

        for row in rows:
            # Safely read each column by index with a fallback
            def _cell(idx):
                return _normalise_str(row[idx]) if idx < len(row) else ''

            row_hu    = _cell(_ML_COL_HU)
            row_part  = _cell(_ML_COL_INDUS)
            row_cust  = _cell(_ML_COL_CUST)
            row_qty   = _cell(_ML_COL_QTY)
            row_move  = _cell(_ML_COL_MOVE_TYPE)
            row_valid = _cell(_ML_COL_VALID_STAT)

            # Skip rows that are already overrides — they are intentional
            if row_valid == 'OVERRIDE':
                continue

            if (row_hu   == n_hu   and
                row_part == n_part and
                row_cust == n_cust and
                row_qty  == n_qty  and
                row_move == n_move):
                return (
                    f"This exact movement was already recorded "
                    f"(HU {hu}, {indus_part}, qty {pallet_qty}, {movement}). "
                    f"Use supervisor override if this is intentional."
                )
        return None

    except Exception as e:
        # Fix 3: fail closed — block the movement with a friendly message
        # rather than silently allowing a potential duplicate through.
        logger.warning(f"Duplicate log check failed (blocking movement): {e}")
        return (
            "System is temporarily busy. Please try again in a moment."
        )


def _check_transition(hu, movement, cur_state, is_override,
                      indus_part='', pallet_qty='', current_row=None,
                      incoming_location=''):
    """
    Validate the movement transition using Pallet_Status.

    STOCK_IN:
      - Blocked if the same HU + Indus_Part + Pallet_Qty combo is already IN.
      - Different Indus_Part or Qty on the same HU = new pallet run, allowed.
      - No location restriction at STOCK_IN time.

    STOCK_OUT:
      - Blocked if already OUT or never seen.
      - If the HU is currently IN, the outgoing Indus_Part and Pallet_Qty are
        compared against the stored values. A mismatch means the worker may be
        scanning the wrong label — blocked unless supervisor override.
      - Location lock: the selected location must match Last_Location in
        Pallet_Status (where it was last stocked IN). Override bypasses this.
    """
    if movement == 'STOCK_IN' and cur_state == 'STOCK_IN':
        if is_override:
            return None
        if current_row:
            stored_part = _normalise_str(current_row.get('Indus_Part', ''))
            stored_qty  = _normalise_str(current_row.get('Pallet_Qty', ''))
            if (stored_part == _normalise_str(indus_part) and
                    stored_qty == _normalise_str(pallet_qty)):
                return (
                    f"Pallet {hu} ({indus_part}, qty {pallet_qty}) is already "
                    f"IN the warehouse. Do Stock OUT first, or use supervisor override."
                )
            # Different part or qty on same HU = allow as a new run
            return None
        return (
            f"Pallet {hu} is already IN the warehouse. "
            f"Do Stock OUT first, or use supervisor override."
        )

    if movement == 'STOCK_OUT':
        if cur_state == 'STOCK_OUT':
            if is_override:
                return None
            return (f"Pallet {hu} is already OUT of the warehouse. "
                    f"Use supervisor override if needed.")

        if cur_state in ('NONE', '', 'NOT IN SYSTEM', 'UNKNOWN'):
            if is_override:
                return None
            return f"Pallet {hu} was never stocked IN. Do Stock IN first."

        # HU is currently IN — verify the outgoing scan matches stored identity.
        # Protects against a worker accidentally scanning the wrong label.
        if current_row and not is_override:
            stored_part = _normalise_str(current_row.get('Indus_Part', ''))
            stored_qty  = _normalise_str(current_row.get('Pallet_Qty', ''))
            incoming_part = _normalise_str(indus_part)
            incoming_qty  = _normalise_str(pallet_qty)
            if stored_part and stored_part != incoming_part:
                return (
                    f"Pallet {hu} is recorded as {current_row.get('Indus_Part')} "
                    f"(qty {current_row.get('Pallet_Qty')}) but the scanned label shows "
                    f"{indus_part} (qty {pallet_qty}). "
                    f"Check the label, or use supervisor override."
                )
            if stored_qty and stored_qty != incoming_qty:
                return (
                    f"Pallet {hu} was stocked IN with qty {current_row.get('Pallet_Qty')} "
                    f"but the scanned label shows qty {pallet_qty}. "
                    f"Check the label, or use supervisor override."
                )

            # Location lock: STOCK_OUT must come from the same location as the last STOCK_IN.
            # cur_state is 'STOCK_IN' here (passed all earlier guards).
            stored_location = _normalise_str(current_row.get('Last_Location', ''))
            if stored_location and stored_location != _normalise_str(incoming_location):
                return (
                    f"Pallet {hu} was stocked IN at {stored_location}. "
                    f"You must do Stock OUT from {stored_location}, not {incoming_location}. "
                    f"Use supervisor override if this is intentional."
                )

    return None


def _fmt_timestamp(iso_str):
    """
    Convert an incoming timestamp to Dubai time (Asia/Dubai).
    Output: DD/MM/YYYY HH:MM:SS AM/PM
    """
    if not iso_str:
        return iso_str
    try:
        from zoneinfo import ZoneInfo
        s = str(iso_str).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dubai_tz = ZoneInfo("Asia/Dubai")
        dt_dubai = dt.astimezone(dubai_tz)
        return dt_dubai.strftime('%d/%m/%Y %I:%M:%S %p')
    except Exception:
        return str(iso_str)


def _log_error(stage, error_code, handling_unit, message):
    """Best-effort write to Error_Log. Never raises."""
    try:
        sheet = current_app.config.get('SHEET_ERROR_LOG', 'Error_Log')
        eid = str(uuid.uuid4())[:12].upper()
        now = datetime.now(timezone.utc).isoformat()
        row = [eid, _fmt_timestamp(now), str(handling_unit), stage, error_code, str(message)[:500], 'NO']
        append_row(sheet, row, ERROR_LOG_COLS)
    except Exception as e:
        logger.error(f"Error_Log write failed: {e}. Original: {stage}/{error_code}/{handling_unit}/{message}")
