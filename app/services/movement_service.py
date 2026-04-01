"""
Core business logic — lookup, movement, state transitions, error logging.

Exact column mapping from Indus Warehouse Stock Movement.xlsx:

Master_Parts (7 cols A-G):
  A=Handling_Unit  B=Indus_Part  C=Customer_Part  D=Pallet_Qty
  E=Job_No  F=Description  G=Active_Flag

Movement_Log (12 cols A-L):
  A=Event_ID  B=Timestamp_UTC  C=Handling_Unit  D=Indus_Part
  E=Customer_Part  F=Pallet_Qty  G=Job_No  H=Movement_Type
  I=Operator_ID  J=Device_ID  K=Sync_Status  L=Validation_Status

Pallet_Status (5 cols A-E):
  A=Handling_Unit  B=Current_Status  C=Last_Event_ID
  D=Last_Movement_Type  E=Last_Movement_Time

Error_Log (7 cols A-G):
  A=Error_ID  B=Timestamp_UTC  C=Handling_Unit  D=Stage
  E=Error_Code  F=Error_Message  G=Resolved_Flag

Every operation ends in one of:
  success_synced | success_pending_sync | rejected_validation | failed_write_logged
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
from app.utils.validators import validate_handling_unit, validate_movement_type, sanitize_text
from app.utils.idempotency import make_key, reserve, release

logger = logging.getLogger(__name__)

# Column counts per sheet (for append/update operations)
MASTER_PARTS_COLS = 7
MOVEMENT_LOG_COLS = 12
PALLET_STATUS_COLS = 5
ERROR_LOG_COLS = 7


class Result:
    """Standardized operation result."""
    def __init__(self, success, status, message, data=None):
        self.success = success
        self.status = status
        self.message = message
        self.data = data or {}

    def to_dict(self):
        return {"success": self.success, "status": self.status,
                "message": self.message, "data": self.data}


# ═══════════════════════════════════════════════════════════════════════════════
#  PALLET LOOKUP
# ═══════════════════════════════════════════════════════════════════════════════

def lookup_pallet(handling_unit):
    """Look up pallet from Master_Parts + current status from Pallet_Status."""
    ok, val = validate_handling_unit(handling_unit)
    if not ok:
        _log_error('LOOKUP', 'VALIDATION_FAIL', handling_unit, val)
        return Result(False, 'rejected_validation', val)
    hu = val

    try:
        sheet = current_app.config['SHEET_MASTER_PARTS']
        pallet = find_row_as_dict(sheet, 'Handling_Unit', hu)

        if not pallet:
            _log_error('LOOKUP', 'UNKNOWN_HU', hu, f"HU {hu} not found in Master_Parts")
            return Result(False, 'rejected_validation',
                          f"Unknown pallet: {hu}. This barcode is not in the system.")

        if str(pallet.get('Active_Flag', '')).strip().upper() != 'YES':
            _log_error('LOOKUP', 'INACTIVE_PALLET', hu, f"HU {hu} Active_Flag != YES")
            return Result(False, 'rejected_validation',
                          f"Pallet {hu} is INACTIVE. Contact supervisor.")

        # Current warehouse status
        status_sheet = current_app.config['SHEET_PALLET_STATUS']
        status = find_row_as_dict(status_sheet, 'Handling_Unit', hu)

        data = {
            'handling_unit': hu,
            'indus_part': _s(pallet.get('Indus_Part')),
            'customer_part': _s(pallet.get('Customer_Part')),
            'pallet_qty': _s(pallet.get('Pallet_Qty')),
            'job_no': _s(pallet.get('Job_No')),
            'description': _s(pallet.get('Description')),
            'current_status': _s(status.get('Current_Status', 'UNKNOWN')) if status else 'NOT IN SYSTEM',
            'last_movement_type': _s(status.get('Last_Movement_Type', 'NONE')) if status else 'NONE',
            'last_movement_time': _s(status.get('Last_Movement_Time', '')) if status else '',
        }
        return Result(True, 'success_synced', 'Pallet found.', data)

    except GraphAPIError as e:
        _log_error('LOOKUP', 'GRAPH_ERROR', hu, e.message)
        return Result(False, 'failed_write_logged',
                      "Cannot reach the system. Try again or work offline.",
                      {'error_details': e.message})
    except Exception as e:
        logger.exception(f"Unexpected lookup error: {e}")
        _log_error('LOOKUP', 'UNEXPECTED', handling_unit, str(e))
        return Result(False, 'failed_write_logged', "Something went wrong. Try again.")


# ═══════════════════════════════════════════════════════════════════════════════
#  PROCESS MOVEMENT
# ═══════════════════════════════════════════════════════════════════════════════

def process_movement(
    handling_unit,
    movement_type,
    operator_id="WORKER",
    device_id="",
    notes="",
    is_override=False,
    override_reason="",
    client_timestamp="",
    idempotency_key="",
):
    """Validate → check state → write Movement_Log → update Pallet_Status."""

    # ── Validate ──
    ok_hu, hu = validate_handling_unit(handling_unit)
    if not ok_hu:
        _log_error('MOVEMENT', 'VALIDATION_FAIL', handling_unit, hu)
        return Result(False, 'rejected_validation', hu)

    ok_mt, movement = validate_movement_type(movement_type)
    if not ok_mt:
        _log_error('MOVEMENT', 'TYPE_INVALID', hu, movement)
        return Result(False, 'rejected_validation', movement)

    now_utc = datetime.now(timezone.utc)
    timestamp = client_timestamp or now_utc.isoformat()
    event_id = str(uuid.uuid4())[:12].upper()

    # ── Idempotency ──
    idem = idempotency_key or make_key(hu, movement, timestamp)
    if not reserve(idem):
        _log_error('MOVEMENT', 'DUPLICATE', hu, f"Duplicate blocked: {idem}")
        return Result(False, 'rejected_validation',
                      "This movement was already recorded. No duplicate created.",
                      {'idempotency_key': idem})

    try:
        # ── Verify pallet exists & is active ──
        master_sheet = current_app.config['SHEET_MASTER_PARTS']
        pallet = find_row_as_dict(master_sheet, 'Handling_Unit', hu)

        if not pallet:
            release(idem)
            _log_error('MOVEMENT', 'UNKNOWN_HU', hu, f"Movement on unknown HU {hu}")
            return Result(False, 'rejected_validation', f"Unknown pallet: {hu}.")

        if str(pallet.get('Active_Flag', '')).strip().upper() != 'YES':
            release(idem)
            _log_error('MOVEMENT', 'INACTIVE', hu, f"Movement on inactive HU {hu}")
            return Result(False, 'rejected_validation', f"Pallet {hu} is INACTIVE.")

        # ── State transition check ──
        status_sheet = current_app.config['SHEET_PALLET_STATUS']
        current = find_row_as_dict(status_sheet, 'Handling_Unit', hu)
        cur_state = _s(current.get('Last_Movement_Type', 'NONE')).upper() if current else 'NONE'

        err = _check_transition(hu, movement, cur_state, is_override)
        if err:
            release(idem)
            _log_error('MOVEMENT', 'INVALID_TRANSITION', hu,
                       f"{movement} blocked. state={cur_state} override={is_override}")
            return Result(False, 'rejected_validation', err,
                          {'current_state': cur_state, 'attempted': movement})

        # ── Determine validation status ──
        if is_override:
            validation_status = "OVERRIDE"
        else:
            validation_status = "VALID"

        # ── Write Movement_Log (append-only, 12 columns A-L) ──
        log_sheet = current_app.config['SHEET_MOVEMENT_LOG']
        log_row = [
            event_id,                                   # A: Event_ID
            timestamp,                                  # B: Timestamp_UTC
            hu,                                         # C: Handling_Unit
            _s(pallet.get('Indus_Part')),               # D: Indus_Part
            _s(pallet.get('Customer_Part')),             # E: Customer_Part
            _s(pallet.get('Pallet_Qty')),               # F: Pallet_Qty
            _s(pallet.get('Job_No')),                   # G: Job_No
            movement,                                   # H: Movement_Type
            operator_id,                                # I: Operator_ID
            device_id,                                  # J: Device_ID
            'SYNCED',                                   # K: Sync_Status
            validation_status,                          # L: Validation_Status
        ]
        append_row(log_sheet, log_row, MOVEMENT_LOG_COLS)

        # ── Upsert Pallet_Status (5 columns A-E) ──
        location = 'IN_WAREHOUSE' if movement == 'STOCK_IN' else 'OUT_OF_WAREHOUSE'
        status_row = [
            hu,             # A: Handling_Unit
            location,       # B: Current_Status
            event_id,       # C: Last_Event_ID
            movement,       # D: Last_Movement_Type
            timestamp,      # E: Last_Movement_Time
        ]
        # key_col_index=0 → Handling_Unit is column A (index 0)
        upsert_row(status_sheet, 0, hu, status_row, PALLET_STATUS_COLS)

        logger.info(f"Movement OK: {movement} HU={hu} event={event_id}")

        display_type = 'Stock IN' if movement == 'STOCK_IN' else 'Stock OUT'
        return Result(True, 'success_synced', f"{display_type} recorded.",
                      {
                          'handling_unit': hu,
                          'movement_type': movement,
                          'event_id': event_id,
                          'timestamp': timestamp,
                          'location': location,
                          'idempotency_key': idem,
                          'indus_part': _s(pallet.get('Indus_Part')),
                          'customer_part': _s(pallet.get('Customer_Part')),
                          'pallet_qty': _s(pallet.get('Pallet_Qty')),
                          'job_no': _s(pallet.get('Job_No')),
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
    """Re-process an event stored offline on the phone."""
    return process_movement(
        handling_unit=event.get('handling_unit', ''),
        movement_type=event.get('movement_type', ''),
        operator_id=event.get('operator_id', 'OFFLINE_WORKER'),
        device_id=event.get('device_id', ''),
        notes=event.get('notes', ''),
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
    """Safe string conversion — never returns None."""
    if val is None:
        return ''
    return str(val)


def _check_transition(hu, movement, cur_state, is_override):
    """Returns error message or None if valid."""
    if movement == 'STOCK_IN' and cur_state == 'STOCK_IN':
        if is_override:
            return None
        return f"Pallet {hu} is already IN the warehouse. Use supervisor override if needed."

    if movement == 'STOCK_OUT':
        if cur_state == 'STOCK_OUT':
            if is_override:
                return None
            return f"Pallet {hu} is already OUT. Use supervisor override if needed."
        if cur_state in ('NONE', '', 'NOT IN SYSTEM', 'UNKNOWN'):
            if is_override:
                return None
            return f"Pallet {hu} was never stocked IN. Do Stock IN first."

    return None


def _log_error(stage, error_code, handling_unit, message):
    """Best-effort write to Error_Log (7 columns A-G). Never raises."""
    try:
        sheet = current_app.config.get('SHEET_ERROR_LOG', 'Error_Log')
        eid = str(uuid.uuid4())[:12].upper()
        now = datetime.now(timezone.utc).isoformat()
        row = [
            eid,                        # A: Error_ID
            now,                        # B: Timestamp_UTC
            str(handling_unit),         # C: Handling_Unit
            stage,                      # D: Stage
            error_code,                 # E: Error_Code
            str(message)[:500],         # F: Error_Message
            'NO',                       # G: Resolved_Flag
        ]
        append_row(sheet, row, ERROR_LOG_COLS)
    except Exception as e:
        logger.error(f"Error_Log write failed: {e}. Original: {stage}/{error_code}/{handling_unit}/{message}")
