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

Movement_Log (12 cols A-L):
  A=Event_ID  B=Timestamp_UTC  C=Handling_Unit  D=Indus_Part
  E=Customer_Part  F=Pallet_Qty  G=Job_No  H=Movement_Type
  I=Operator_ID  J=Device_ID  K=Sync_Status  L=Validation_Status

Pallet_Status (7 cols A-G):
  A=Handling_Unit  B=Current_Status  C=Last_Event_ID
  D=Last_Movement_Type  E=Last_Movement_Time  F=Indus_Part  G=Pallet_Qty

Error_Log (7 cols A-G):
  A=Error_ID  B=Timestamp_UTC  C=Handling_Unit  D=Stage
  E=Error_Code  F=Error_Message  G=Resolved_Flag
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
from app.utils.validators import validate_handling_unit, validate_movement_type
from app.utils.idempotency import make_key, reserve, release

logger = logging.getLogger(__name__)

MOVEMENT_LOG_COLS = 12
PALLET_STATUS_COLS = 7
ERROR_LOG_COLS = 7


class Result:
    def __init__(self, success, status, message, data=None):
        self.success = success
        self.status = status
        self.message = message
        self.data = data or {}

    def to_dict(self):
        return {"success": self.success, "status": self.status,
                "message": self.message, "data": self.data}


# ═══════════════════════════════════════════════════════════════════════════════
#  PROCESS MOVEMENT — data comes from scanned label, no master lookup
# ═══════════════════════════════════════════════════════════════════════════════

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
):
    """
    Record a Stock IN / Stock OUT movement.
    All pallet data (indus_part, customer_part, pallet_qty) comes from the scanned label.
    No master data lookup needed.
    """

    # ── Validate HU ──
    ok_hu, hu = validate_handling_unit(handling_unit)
    if not ok_hu:
        _log_error('MOVEMENT', 'VALIDATION_FAIL', handling_unit, hu)
        return Result(False, 'rejected_validation', hu)

    ok_mt, movement = validate_movement_type(movement_type)
    if not ok_mt:
        _log_error('MOVEMENT', 'TYPE_INVALID', hu, movement)
        return Result(False, 'rejected_validation', movement)

    # Basic check: indus_part should not be empty
    if not indus_part or not str(indus_part).strip():
        _log_error('MOVEMENT', 'MISSING_INDUS_PART', hu, 'Indus Part not scanned')
        return Result(False, 'rejected_validation', 'Indus Part is missing. Scan the Indus Part QR code.')

    now_utc = datetime.now(timezone.utc)
    timestamp = client_timestamp or now_utc.isoformat()
    event_id = str(uuid.uuid4())[:12].upper()

    # ── Idempotency ──
    idem = idempotency_key or make_key(hu, movement, timestamp)
    if not reserve(idem):
        _log_error('MOVEMENT', 'DUPLICATE', hu, f"Duplicate blocked: {idem}")
        return Result(False, 'rejected_validation',
                      "This movement was already recorded.",
                      {'idempotency_key': idem})

    try:
        # ── State transition check (from Pallet_Status) ──
        status_sheet = current_app.config['SHEET_PALLET_STATUS']
        current = find_row_as_dict(status_sheet, 'Handling_Unit', hu)
        cur_state = _s(current.get('Last_Movement_Type', 'NONE')).upper() if current else 'NONE'

        err = _check_transition(hu, movement, cur_state, is_override,
                                indus_part=indus_part,
                                pallet_qty=pallet_qty,
                                current_row=current)
        if err:
            release(idem)
            _log_error('MOVEMENT', 'INVALID_TRANSITION', hu,
                       f"{movement} blocked. state={cur_state} override={is_override}")
            return Result(False, 'rejected_validation', err,
                          {'current_state': cur_state, 'attempted': movement})

        # ── Determine validation status ──
        validation_status = "OVERRIDE" if is_override else "VALID"

        # ── Write Movement_Log (append-only, 12 columns A-L) ──
        log_sheet = current_app.config['SHEET_MOVEMENT_LOG']
        log_row = [
            event_id,                           # A: Event_ID
            _fmt_timestamp(timestamp),          # B: Timestamp_UTC
            hu,                                 # C: Handling_Unit
            _s(indus_part).strip(),             # D: Indus_Part
            _s(customer_part).strip(),          # E: Customer_Part
            _s(pallet_qty).strip(),             # F: Pallet_Qty
            _s(job_no).strip(),                 # G: Job_No
            movement,                           # H: Movement_Type
            operator_id,                        # I: Operator_ID
            device_id,                          # J: Device_ID
            'SYNCED',                           # K: Sync_Status
            validation_status,                  # L: Validation_Status
        ]
        append_row(log_sheet, log_row, MOVEMENT_LOG_COLS)

        # ── Upsert Pallet_Status (7 columns A-G) ──
        location = 'IN_WAREHOUSE' if movement == 'STOCK_IN' else 'OUT_OF_WAREHOUSE'
        status_row = [hu, location, event_id, movement, _fmt_timestamp(timestamp),
                      _s(indus_part).strip(), _s(pallet_qty).strip()]
        upsert_row(status_sheet, 0, hu, status_row, PALLET_STATUS_COLS)

        logger.info(f"Movement OK: {movement} HU={hu} part={indus_part} event={event_id}")

        display_type = 'Stock IN' if movement == 'STOCK_IN' else 'Stock OUT'
        return Result(True, 'success_synced', f"{display_type} recorded.",
                      {
                          'handling_unit': hu,
                          'movement_type': movement,
                          'event_id': event_id,
                          'timestamp': timestamp,
                          'location': location,
                          'idempotency_key': idem,
                          'indus_part': _s(indus_part),
                          'customer_part': _s(customer_part),
                          'pallet_qty': _s(pallet_qty),
                          'job_no': _s(job_no),
                          'is_override': is_override,
                      })

    except GraphAPIError as e:
        release(idem)
        _log_error('MOVEMENT', 'GRAPH_ERROR', hu, f"Graph write failed: {e.message}")
        return Result(False, 'failed_write_logged',
                      "Could not save. Saved locally — will sync automatically.",
                      {'idempotency_key': idem, 'needs_sync': True})
    except Exception as e:
        release(idem)
        logger.exception(f"Unexpected movement error: {e}")
        _log_error('MOVEMENT', 'UNEXPECTED', hu, str(e))
        return Result(False, 'failed_write_logged',
                      "Something went wrong. Saved locally.",
                      {'idempotency_key': idem, 'needs_sync': True})


# ═══════════════════════════════════════════════════════════════════════════════
#  OFFLINE SYNC
# ═══════════════════════════════════════════════════════════════════════════════

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
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  ADMIN QUERIES
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
#  PRIVATE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _s(val):
    if val is None:
        return ''
    return str(val)


def _check_transition(hu, movement, cur_state, is_override,
                      indus_part='', pallet_qty='', current_row=None):
    """
    Validate the movement transition.

    For STOCK_IN: blocked if the same HU + Indus Part + Pallet Qty combo
    is already IN. Different Indus Part or Qty = treated as a new pallet run.

    For STOCK_OUT: blocked if already OUT or never seen.
    """
    if movement == 'STOCK_IN' and cur_state == 'STOCK_IN':
        if is_override:
            return None
        # Check if it's the SAME combination of HU + Indus Part + Pallet Qty
        if current_row:
            stored_part = _normalise_str(current_row.get('Indus_Part', ''))
            stored_qty  = _normalise_str(current_row.get('Pallet_Qty', ''))
            incoming_part = _normalise_str(indus_part)
            incoming_qty  = _normalise_str(pallet_qty)
            # Only block if ALL three match (HU already matched via cur_state lookup)
            if stored_part == incoming_part and stored_qty == incoming_qty:
                return (
                    f"Pallet {hu} ({indus_part}, qty {pallet_qty}) is already "
                    f"IN the warehouse. Do Stock OUT first, or use supervisor override."
                )
            # Different part or qty on same HU = allow (different run)
            return None
        # No stored row data available — block on HU alone to be safe
        return (
            f"Pallet {hu} is already IN the warehouse. "
            f"Do Stock OUT first, or use supervisor override."
        )

    if movement == 'STOCK_OUT':
        if cur_state == 'STOCK_OUT':
            if is_override:
                return None
            return f"Pallet {hu} is already OUT of the warehouse. Use supervisor override if needed."
        if cur_state in ('NONE', '', 'NOT IN SYSTEM', 'UNKNOWN'):
            if is_override:
                return None
            return f"Pallet {hu} was never stocked IN. Do Stock IN first."

    return None

def _fmt_timestamp(iso_str):
    """
    Convert an incoming timestamp to Dubai time (Asia/Dubai) and store it as:
    DD/MM/YYYY HH:MM:SS AM/PM

    Accepted inputs:
    - 2026-04-01T12:48:17.802Z
    - 2026-04-01T12:48:17+00:00
    - 2026-04-01 12:48:17
    - already-naive timestamps (assumed UTC)

    Output example:
    - 01/04/2026 04:48:17 PM
    """
    if not iso_str:
        return iso_str

    try:
        from zoneinfo import ZoneInfo

        s = str(iso_str).strip()

        # Normalize common UTC forms
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"

        # Parse the timestamp
        dt = datetime.fromisoformat(s)

        # If no timezone info exists, assume UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        # Convert to Dubai time
        dubai_tz = ZoneInfo("Asia/Dubai")
        dt_dubai = dt.astimezone(dubai_tz)

        return dt_dubai.strftime('%d/%m/%Y %I:%M:%S %p')

    except Exception:
        return str(iso_str)

def _normalise_str(val):
    """Strip, uppercase, remove trailing .0 from numeric strings."""
    s = str(val).strip().upper() if val is not None else ''
    # Handle '39.0' → '39' (Excel float leakage)
    if s.endswith('.0') and s[:-2].isdigit():
        s = s[:-2]
    return s

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
