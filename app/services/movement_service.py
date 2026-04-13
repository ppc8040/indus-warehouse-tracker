"""
Core business logic — scan-to-log architecture.

NO Master_Parts lookup. All pallet data comes from the SAP HU lookup
or Pallet_Status (whichever takes precedence via /api/lookup-hu):
  - Barcode (Code 128): Handling_Unit (e.g. 100000000000839157)
  - SAP API / Pallet_Status: Indus_Part, Pallet_Qty

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
  Duplicate blocking is done by querying Movement_Log directly.
  A movement is a duplicate when a row already exists with the same
  (Handling_Unit, Movement_Type) combination — regardless of whether
  the previous record was an override or not.

  Exception: if the most-recent matching row has Movement_Type=STOCK_IN
  and the new movement is STOCK_OUT (or vice versa), it is NOT a duplicate
  — it is a valid continuation of the IN/OUT cycle.  The transition check
  (Pallet_Status) handles this case separately.

  This rule applies to ALL movements, including supervisor overrides.
  An override bypasses transition checks but never bypasses duplicate checks.

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
  '100000000000839157) so Excel treats them as text and does not corrupt
  the 18-digit number as a float. The Graph API returns the raw value
  without the apostrophe. The _normalise_cell helper in graph_client.py
  strips whitespace and uppercases, so all HU comparisons work correctly.

Session strategy:
  process_movement() opens ONE workbook session at the start, forwards it
  to every graph_client _impl call via the ext_session parameter, and closes
  it exactly once in a finally block.  This keeps the total concurrent open
  sessions at 1 per in-flight movement request, well below Microsoft Graph's
  hard limit of 5 per workbook.

  All other callers (_log_error, admin queries, offline sync, lookup-hu) use
  the standalone public wrappers in graph_client.py which each manage their
  own single session — no changes needed at those call sites.
"""
import uuid
import logging
from datetime import datetime, timezone

from flask import current_app
from app.services.graph_client import (
    _open_session,
    _close_session,
    _read_all_rows_impl,
    _find_row_as_dict_impl,
    _append_row_impl,
    _upsert_row_impl,
    find_row_as_dict,
    append_row,
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

# Movement_Log column positions (0-based)
_ML_COL_HU         = 2   # C: Handling_Unit
_ML_COL_INDUS      = 3   # D: Indus_Part
_ML_COL_CUST       = 4   # E: Customer_Part
_ML_COL_QTY        = 5   # F: Pallet_Qty
_ML_COL_MOVE_TYPE  = 7   # H: Movement_Type
_ML_COL_VALID_STAT = 11  # L: Validation_Status

# Excel's epoch: serial 1 = 1900-01-01, but Excel incorrectly treats
# 1900 as a leap year, so the practical epoch is 1899-12-30.
_EXCEL_EPOCH = datetime(1899, 12, 30, tzinfo=timezone.utc)


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
#  PROCESS MOVEMENT
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
    Duplicate check runs for ALL movements, including supervisor overrides.
    Transition check is bypassed for overrides.

    Uses a single shared workbook session for all Graph operations to stay
    within Microsoft's 5-concurrent-sessions limit.
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
        _log_error('MOVEMENT', 'MISSING_LOCATION', hu,
                   f'Invalid or missing location: {location}')
        return Result(False, 'rejected_validation',
                      'Location is required. Select a valid location '
                      '(M4, M5, M6, M4_STORE, M16_STORE).')

    now_utc = datetime.now(timezone.utc)
    timestamp = client_timestamp or now_utc.isoformat()
    event_id = str(uuid.uuid4())[:12].upper()

    # ── Open ONE session for the entire movement ──
    # All Graph calls below share this session to avoid hitting Microsoft's
    # 5-concurrent-sessions-per-workbook hard limit.
    session = None
    try:
        session = _open_session()
        log_sheet    = current_app.config['SHEET_MOVEMENT_LOG']
        status_sheet = current_app.config['SHEET_PALLET_STATUS']

        # ── Duplicate check — runs for ALL movements (including overrides) ──
        # An override bypasses TRANSITION checks only, never duplicate checks.
        dup_err = _check_duplicate_in_log(log_sheet, hu, movement, ext_session=session)
        if dup_err:
            _log_error('MOVEMENT', 'DUPLICATE', hu, dup_err)
            return Result(False, 'rejected_duplicate', dup_err,
                          {'attempted': movement})

        # ── State transition check (skipped for overrides) ──
        if not is_override:
            current = _find_row_as_dict_impl(status_sheet, 'Handling_Unit', hu,
                                             ext_session=session)
            cur_state = _s(current.get('Last_Movement_Type', 'NONE')).upper() \
                if current else 'NONE'

            err = _check_transition(hu, movement, cur_state, is_override,
                                    indus_part=indus_clean,
                                    pallet_qty=qty_clean,
                                    current_row=current,
                                    incoming_location=loc)
            if err:
                _log_error('MOVEMENT', 'INVALID_TRANSITION', hu,
                           f"{movement} blocked. state={cur_state} override={is_override}")
                return Result(False, 'rejected_transition', err,
                              {'current_state': cur_state, 'attempted': movement})
        else:
            # Override: still fetch current row for the upsert below,
            # but skip all transition/location checks.
            current = _find_row_as_dict_impl(status_sheet, 'Handling_Unit', hu,
                                             ext_session=session)

        # ── Determine validation status ──
        validation_status = "OVERRIDE" if is_override else "VALID"

        # ── Write Movement_Log (append-only, 13 columns A-M) ──
        log_row = [
            event_id,
            _fmt_timestamp(timestamp),
            hu,
            indus_clean,
            cust_clean,
            qty_clean,
            _s(job_no).strip(),
            movement,
            operator_id,
            device_id,
            'SYNCED',
            validation_status,
            loc,
        ]
        _append_row_impl(log_sheet, log_row, MOVEMENT_LOG_COLS, ext_session=session)

        # ── Upsert Pallet_Status (8 columns A-H) ──
        current_status = 'IN_WAREHOUSE' if movement == 'STOCK_IN' else 'OUT_OF_WAREHOUSE'
        status_row = [
            hu,
            current_status,
            event_id,
            movement,
            _fmt_timestamp(timestamp),
            indus_clean,
            qty_clean,
            loc,
        ]
        _upsert_row_impl(status_sheet, 0, hu, status_row, PALLET_STATUS_COLS,
                         ext_session=session)

        logger.info(
            "Movement OK: %s HU=%s part=%s location=%s event=%s",
            movement, hu, indus_clean, loc, event_id
        )

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
        _log_error('MOVEMENT', 'GRAPH_ERROR', hu,
                   f"Graph write failed: {e.message}")
        return Result(False, 'failed_write_logged',
                      "Could not save. Saved locally — will sync automatically.",
                      {'needs_sync': True})
    except Exception as e:
        logger.exception(f"Unexpected movement error: {e}")
        _log_error('MOVEMENT', 'UNEXPECTED', hu, str(e))
        return Result(False, 'failed_write_logged',
                      "Something went wrong. Saved locally.",
                      {'needs_sync': True})
    finally:
        # Always close the shared session, even on error paths.
        if session is not None:
            _close_session(session[0])


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
    Also strips a leading apostrophe that Excel inserts as a text-prefix.
    """
    s = str(val).strip() if val is not None else ''
    if s.startswith("'"):
        s = s[1:]
    s = s.upper()
    if s.endswith('.0') and s[:-2].isdigit():
        s = s[:-2]
    return s


def _check_duplicate_in_log(log_sheet, hu, movement, ext_session=None):
    """
    Query Movement_Log to see if the same HU has already been recorded
    with the same Movement_Type.

    Accepts an optional ext_session tuple so it can participate in the
    caller's shared session instead of opening a new one.

    This check applies to ALL movements — including supervisor overrides.
    An override bypasses transition checks only; it never bypasses duplicate
    detection.  This prevents the same pallet being accidentally double-scanned
    (or intentionally re-submitted) for the same direction.

    Logic:
      - Scan all rows in Movement_Log.
      - Find rows where Handling_Unit matches.
      - Among those, find the most-recent row by index (last row = most recent
        since the log is append-only).
      - If the most-recent matching row has the same Movement_Type as the
        incoming movement → duplicate, block it.
      - If the most-recent matching row has the OPPOSITE Movement_Type
        (e.g. last was STOCK_IN, now doing STOCK_OUT) → legitimate cycle
        continuation, allow through.  The transition check handles the
        rest of the business rules for this case.
      - If no matching row exists → brand new HU, allow through.

    Fails closed: on Excel read error, returns a friendly retry message
    instead of None so that no movement silently passes through.

    Returns an error message string if duplicate found or read failed,
    or None if clean.
    """
    try:
        headers, rows = _read_all_rows_impl(log_sheet, ext_session=ext_session)

        n_hu   = _normalise_str(hu)
        n_move = _normalise_str(movement)

        last_matching_move = None  # Movement_Type of the most-recent HU row

        for row in rows:
            def _cell(idx):
                return _normalise_str(row[idx]) if idx < len(row) else ''

            row_hu   = _cell(_ML_COL_HU)
            row_move = _cell(_ML_COL_MOVE_TYPE)

            if row_hu == n_hu:
                # Keep updating — we want the LAST (most recent) matching row
                last_matching_move = row_move

        if last_matching_move is None:
            # HU has never been recorded — allow
            return None

        if last_matching_move == n_move:
            # Same direction as the last recorded movement → duplicate
            direction = 'Stock IN' if n_move == 'STOCK_IN' else 'Stock OUT'
            return (
                f"HU {hu} was already recorded as {direction}. "
                f"Scan a different pallet, or use supervisor override "
                f"if this is intentional."
            )

        # Opposite direction (IN→OUT or OUT→IN) → legitimate cycle, allow
        return None

    except Exception as e:
        logger.warning(f"Duplicate log check failed (blocking movement): {e}")
        return (
            "System is temporarily busy. Please try again in a moment."
        )


def _check_transition(hu, movement, cur_state, is_override,
                      indus_part='', pallet_qty='', current_row=None,
                      incoming_location=''):
    """
    Validate the movement transition using Pallet_Status.
    Only called when is_override=False.

    STOCK_IN:
      - Blocked if the same HU + Indus_Part + Pallet_Qty combo is already IN.
      - Different Indus_Part or Qty on the same HU = new pallet run, allowed.

    STOCK_OUT:
      - Blocked if already OUT or never seen.
      - If the HU is currently IN, outgoing part/qty are compared with stored
        values.  Mismatch → blocked (wrong label guard).
      - Location lock: outgoing location must match the stored Last_Location.
    """
    if movement == 'STOCK_IN' and cur_state == 'STOCK_IN':
        if current_row:
            stored_part = _normalise_str(current_row.get('Indus_Part', ''))
            stored_qty  = _normalise_str(current_row.get('Pallet_Qty', ''))
            if (stored_part == _normalise_str(indus_part) and
                    stored_qty == _normalise_str(pallet_qty)):
                return (
                    f"Pallet {hu} ({indus_part}, qty {pallet_qty}) is already "
                    f"IN the warehouse. Do Stock OUT first, or use supervisor override."
                )
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
            return f"Pallet {hu} was never stocked IN. Do Stock IN first, or use supervisor override."

        # HU is currently IN — verify outgoing scan matches stored identity
        if current_row:
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
                    f"Pallet {hu} was stocked IN with qty "
                    f"{current_row.get('Pallet_Qty')} "
                    f"but the scanned label shows qty {pallet_qty}. "
                    f"Check the label, or use supervisor override."
                )

            stored_location = _normalise_str(current_row.get('Last_Location', ''))
            if stored_location and \
                    stored_location != _normalise_str(incoming_location):
                return (
                    f"Pallet {hu} was stocked IN at {stored_location}. "
                    f"You must do Stock OUT from {stored_location}, "
                    f"not {incoming_location}. "
                    f"Use supervisor override if this is intentional."
                )

    return None


def _fmt_timestamp(iso_str):
    """
    Convert an incoming ISO timestamp (UTC) to Dubai time (Asia/Dubai)
    and return it as an Excel serial number (float).

    Excel serial = number of days since 1899-12-30 (its effective epoch).
    Sending a float via the Graph API makes Excel store the cell as a
    native datetime value, so the column's number format (D/M/YYYY H:MM)
    displays it correctly — e.g. 12/4/2026 14:53 — and the cell remains
    sortable and filterable.

    Sending a string instead causes Excel to store it as plain text with
    no serial number, breaking date operations.

    Accepted inputs:
      2026-04-13T12:37:00.000Z
      2026-04-13T12:37:00+00:00
      2026-04-13 12:37:00
      (naive timestamps assumed UTC)

    Falls back to the original string on any parse error so nothing breaks.
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

        # Convert to Dubai local time
        dubai_tz = ZoneInfo("Asia/Dubai")
        dt_dubai = dt.astimezone(dubai_tz)

        # Strip timezone info so the delta is computed against the naive epoch
        dt_naive = dt_dubai.replace(tzinfo=None)
        epoch_naive = datetime(1899, 12, 30)
        delta = dt_naive - epoch_naive

        # Return as float — Graph API writes this as an Excel datetime serial
        return delta.total_seconds() / 86400.0

    except Exception:
        # Safe fallback: return original string rather than crashing
        return str(iso_str)


def _log_error(stage, error_code, handling_unit, message):
    """Best-effort write to Error_Log. Never raises.
    Uses the standalone append_row (its own session) intentionally —
    this is called both inside and outside the main movement session scope.
    """
    try:
        sheet = current_app.config.get('SHEET_ERROR_LOG', 'Error_Log')
        eid = str(uuid.uuid4())[:12].upper()
        now = datetime.now(timezone.utc).isoformat()
        row = [eid, _fmt_timestamp(now), str(handling_unit), stage,
               error_code, str(message)[:500], 'NO']
        append_row(sheet, row, ERROR_LOG_COLS)
    except Exception as e:
        logger.error(
            "Error_Log write failed: %s. Original: %s/%s/%s/%s",
            e, stage, error_code, handling_unit, message
        )
