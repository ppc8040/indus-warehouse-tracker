"""
JSON API routes.

Security:
  PIN validation — /validate-pin and /movement (override path) validate
  the supervisor PIN against the configured SUPERVISOR_PIN.
  Both SAP-not-found overrides and movement-rejection overrides use the
  same direct numeric PIN validation. No tokens, no TTL, no replay risk.

  Duplicate guard — _check_duplicate_in_log() applies to ALL movements
  including overrides. Returns status 'rejected_duplicate' so the
  frontend blocks cleanly without offering a supervisor override option.

  Pallet_Status priority — /api/lookup-hu checks Pallet_Status first.
  If the HU is already recorded, stored Material and Qty are returned
  instead of SAP values (handles label vs SAP discrepancies).

  Offline sync — /sync is capped at 50 events per request.

  IVT Line — /api/ivt-check and /api/ivt-enter are completely isolated
  from the warehouse movement flow. They only touch the IVT_Line worksheet.
  Code validation (5 rules) is enforced server-side as a second safety net;
  the frontend performs the same checks client-side first for instant feedback.
"""
import re
import logging
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, current_app
from app.services.movement_service import process_movement, sync_offline_event
from app.utils.validators import validate_supervisor_pin, validate_handling_unit

logger = logging.getLogger(__name__)
api_bp = Blueprint('api', __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_dubai(dt_utc: datetime) -> str:
    """Format a UTC datetime to Dubai time: DD/MM/YYYY HH:MM:SS AM/PM"""
    try:
        from zoneinfo import ZoneInfo
        dubai_tz = ZoneInfo("Asia/Dubai")
        dt_dubai = dt_utc.astimezone(dubai_tz)
        return dt_dubai.strftime('%d/%m/%Y %I:%M:%S %p')
    except Exception:
        return dt_utc.isoformat()


# ---------------------------------------------------------------------------
# IVT code validation
# ---------------------------------------------------------------------------
# Code structure (33 digits total, all numeric):
#   Positions  0–8   (9 digits)  : Customer code — always "561469490"
#   Positions  9–16  (8 digits)  : Date in 00DDMMYY format
#   Positions 17–22  (6 digits)  : Serial number — must be unique per scan
#   Positions 23–32  (10 digits) : Customer part code — "8750722699" or "8750722840"

_IVT_CUSTOMER_CODE   = '561469490'
_IVT_TOTAL_LENGTH    = 33
_IVT_VALID_PART_CODES = {'8750722699', '8750722840'}


def _ivt_validate_code(code: str):
    """
    Validate an IVT QR code string.
    Returns None if valid, or a (error_type, message) tuple if invalid.

    Checks (in order):
      1. Non-numeric characters
      2. Total length must be 33
      3. Customer code segment (pos 0-8)
      4. Date segment (pos 9-16) — 00DDMMYY with valid DD/MM
      5. Customer part code (pos 23-32)
    """
    # 1. Non-numeric characters
    bad = re.search(r'[^0-9]', code)
    if bad:
        ch = bad.group(0)
        return ('invalid_chars',
                f"Invalid character \u2018{ch}\u2019 found in code. "
                f"IVT codes must contain numbers only.")

    # 2. Total length
    if len(code) != _IVT_TOTAL_LENGTH:
        return ('invalid_length',
                f"Code length is {len(code)} digits but must be exactly "
                f"{_IVT_TOTAL_LENGTH} digits.")

    # 3. Customer code
    customer = code[0:9]
    if customer != _IVT_CUSTOMER_CODE:
        return ('invalid_customer_code',
                f"Invalid customer code \u2018{customer}\u2019 "
                f"\u2014 expected {_IVT_CUSTOMER_CODE}.")

    # 4. Date segment — 00DDMMYY; first two chars must be '00',
    #    DD in 01-31, MM in 01-12
    date_seg = code[9:17]
    if date_seg[:2] != '00':
        return ('invalid_date',
                f"Invalid date segment \u2018{date_seg}\u2019 "
                f"\u2014 must start with 00 (format 00DDMMYY).")
    dd = int(date_seg[2:4])
    mm = int(date_seg[4:6])
    if not (1 <= dd <= 31):
        return ('invalid_date',
                f"Invalid day \u2018{date_seg[2:4]}\u2019 in date segment "
                f"\u2018{date_seg}\u2019 \u2014 day must be 01\u201331.")
    if not (1 <= mm <= 12):
        return ('invalid_date',
                f"Invalid month \u2018{date_seg[4:6]}\u2019 in date segment "
                f"\u2018{date_seg}\u2019 \u2014 month must be 01\u201312.")

    # 5. Customer part code
    part_code = code[23:33]
    if part_code not in _IVT_VALID_PART_CODES:
        valid_list = ' or '.join(sorted(_IVT_VALID_PART_CODES))
        return ('invalid_part_code',
                f"Invalid customer part code \u2018{part_code}\u2019 "
                f"\u2014 must be {valid_list}.")

    return None  # all checks passed


# ===========================================================================
# ROUTES
# ===========================================================================

@api_bp.route('/movement', methods=['POST'])
def record_movement():
    """
    Record a Stock IN or Stock OUT movement.

    For any supervisor override (is_override=True), supervisor_pin is
    validated directly as a numeric PIN against SUPERVISOR_PIN config.
    This applies to both SAP-not-found overrides and movement-rejection
    overrides — same simple check for both cases.

    Returns:
      200 — success
      409 — rejected_validation or rejected_duplicate
      502 — write/graph failure
    """
    data = request.get_json(silent=True) or {}
    hu = data.get('handling_unit', '')
    mt = data.get('movement_type', '')

    if not hu or not mt:
        return jsonify({"success": False, "status": "rejected_validation",
                        "message": "Handling Unit and movement type are required."}), 400

    is_override = data.get('is_override', False)
    if is_override:
        pin = data.get('supervisor_pin', '')
        correct = current_app.config.get('SUPERVISOR_PIN', '9999')
        if not validate_supervisor_pin(pin, correct):
            return jsonify({"success": False, "status": "rejected_validation",
                            "message": "Invalid supervisor PIN."}), 403

    result = process_movement(
        handling_unit=hu,
        movement_type=mt,
        indus_part=data.get('indus_part', ''),
        customer_part=data.get('customer_part', ''),
        pallet_qty=data.get('pallet_qty', ''),
        job_no=data.get('job_no', ''),
        operator_id=data.get('operator_id', 'WORKER'),
        device_id=data.get('device_id', ''),
        is_override=is_override,
        override_reason=data.get('override_reason', ''),
        client_timestamp=data.get('timestamp', ''),
        idempotency_key=data.get('idempotency_key', ''),
        location=data.get('location', ''),
    )

    if result.success:
        return jsonify(result.to_dict()), 200
    elif result.status in ('rejected_validation', 'rejected_duplicate'):
        return jsonify(result.to_dict()), 409
    else:
        return jsonify(result.to_dict()), 502


@api_bp.route('/lookup-hu', methods=['POST'])
def lookup_hu():
    """
    Look up a Handling Unit.

    Checks Pallet_Status first. If the HU already exists in the worksheet,
    returns the stored Material and Qty instead of calling SAP.
    This handles label discrepancies (e.g. label says 60PC, SAP says 6PC)
    by trusting the previously-recorded correct values.

    Only falls through to SAP when the HU is not yet in Pallet_Status.
    Returns HU_NOT_FOUND if SAP also has no record.
    """
    data = request.get_json(silent=True) or {}
    hu_raw = data.get('handling_unit', '')

    ok, hu = validate_handling_unit(hu_raw)
    if not ok:
        return jsonify({"success": False, "error_code": "INVALID_HU", "message": hu}), 400

    # Check Pallet_Status before calling SAP
    try:
        from app.services.graph_client import find_row_as_dict
        status_sheet = current_app.config['SHEET_PALLET_STATUS']
        existing = find_row_as_dict(status_sheet, 'Handling_Unit', hu)
        if existing:
            stored_part = str(existing.get('Indus_Part', '') or '').strip().upper()
            stored_qty  = str(existing.get('Pallet_Qty', '') or '').strip()
            # Strip .0 from Excel float leak
            if stored_qty.endswith('.0') and stored_qty[:-2].isdigit():
                stored_qty = stored_qty[:-2]
            if stored_part and stored_qty and stored_qty != '0':
                logger.info(
                    "HU %s found in Pallet_Status — using stored values "
                    "(part=%s qty=%s) instead of SAP",
                    hu, stored_part, stored_qty
                )
                return jsonify({
                    "success": True,
                    "handling_unit": hu,
                    "indus_part": stored_part,
                    "pallet_qty": stored_qty,
                    "source": "pallet_status",
                }), 200
    except Exception as ps_exc:
        # Non-fatal — fall through to SAP lookup
        logger.warning("Pallet_Status check failed (non-fatal), falling back to SAP: %s", ps_exc)

    # SAP lookup (fallback when HU not yet in Pallet_Status)
    try:
        from app.services.sap_client import lookup_hu as sap_lookup
        result = sap_lookup(hu)
        return jsonify({
            "success": True,
            "handling_unit": result["handling_unit"],
            "indus_part": result["indus_part"],
            "pallet_qty": result["pallet_qty"],
            "source": "sap",
        }), 200
    except Exception as exc:
        error_code = getattr(exc, 'error_code', 'SAP_UNKNOWN')
        msg = str(exc)
        logger.error("HU lookup failed for %s [%s]: %s", hu, error_code, msg)
        return jsonify({"success": False, "error_code": error_code, "message": msg}), 502


@api_bp.route('/sync', methods=['POST'])
def sync_offline():
    """Sync offline-queued movements. Capped at 50 events per request."""
    data = request.get_json(silent=True) or {}
    events = data.get('events', [])

    if not events:
        return jsonify({"success": True, "message": "No events to sync.", "results": []}), 200

    if len(events) > 50:
        return jsonify({
            "success": False,
            "message": "Too many events in one sync batch. Maximum is 50."
        }), 400

    results = []
    for ev in events:
        try:
            r = sync_offline_event(ev)
            results.append({
                "idempotency_key": ev.get('idempotency_key', ''),
                "handling_unit": ev.get('handling_unit', ''),
                **r.to_dict()
            })
        except Exception as e:
            logger.error(f"Sync event failed: {e}")
            results.append({
                "idempotency_key": ev.get('idempotency_key', ''),
                "handling_unit": ev.get('handling_unit', ''),
                "success": False, "status": "failed_write_logged", "message": str(e),
            })

    return jsonify({"success": True, "message": f"Processed {len(results)} events.",
                    "results": results}), 200


@api_bp.route('/validate-pin', methods=['POST'])
def validate_pin():
    """
    Validate supervisor PIN.
    Returns valid:true on success. Used by the SAP-not-found override modal
    to confirm the PIN before showing the manual entry fields.
    """
    data = request.get_json(silent=True) or {}
    pin  = data.get('pin', '')
    correct = current_app.config.get('SUPERVISOR_PIN', '9999')

    if validate_supervisor_pin(pin, correct):
        return jsonify({"valid": True}), 200

    return jsonify({"valid": False, "message": "Invalid PIN."}), 403


# ===========================================================================
# IVT LINE ROUTES
# Completely isolated from warehouse movement flow.
# Touches only the IVT_Line worksheet.
#   Column A : Code          (full 33-digit QR value)
#   Column B : Scanned Date  (Dubai time, DD/MM/YYYY HH:MM:SS AM/PM)
#   Column C : Location      (M4 or M5)
# ===========================================================================

_IVT_VALID_LOCATIONS = {'M4', 'M5'}


@api_bp.route('/ivt-check', methods=['POST'])
def ivt_check():
    """
    Validate and duplicate-check an IVT QR code.

    Validation rules (server-side mirror of the client-side checks):
      1. No non-numeric characters
      2. Total length must be exactly 33 digits
      3. Positions 0-8  must equal '561469490'  (customer code)
      4. Positions 9-16 must be valid 00DDMMYY   (date segment)
      5. Positions 23-32 must be '8750722699' or '8750722840'  (part code)

    If validation passes, Column A of IVT_Line is searched for an
    exact match (duplicate check).

    Returns:
      { "valid": false, "error_type": "...", "message": "..." }  — validation failed
      { "valid": true, "duplicate": false }                      — unique, safe to enter
      { "valid": true, "duplicate": true, "scanned_date": "..." } — duplicate found
      400 — missing code
      502 — Graph/Excel read failure
    """
    data = request.get_json(silent=True) or {}
    code = str(data.get('code', '')).strip()

    if not code:
        return jsonify({"success": False, "message": "Code is required."}), 400

    # Server-side validation (safety net — client validates first)
    err = _ivt_validate_code(code)
    if err:
        error_type, message = err
        logger.warning("IVT validation failed for code %r [%s]: %s", code, error_type, message)
        return jsonify({"valid": False, "error_type": error_type, "message": message}), 200

    # Duplicate check against IVT_Line Column A
    try:
        from app.services.graph_client import find_row_as_dict
        sheet = current_app.config['SHEET_IVT_LINE']
        existing = find_row_as_dict(sheet, 'Code', code)

        if existing:
            raw_date = str(existing.get('Scanned Date', '') or '').strip()
            return jsonify({"valid": True, "duplicate": True, "scanned_date": raw_date}), 200

        return jsonify({"valid": True, "duplicate": False}), 200

    except Exception as exc:
        logger.error("IVT check failed for code %s: %s", code, exc)
        return jsonify({"success": False, "message": "Could not read IVT_Line sheet. Try again."}), 502


@api_bp.route('/ivt-enter', methods=['POST'])
def ivt_enter():
    """
    Append a validated, unique IVT code to the IVT_Line worksheet.

    Writes three columns:
      A: code
      B: scanned timestamp (Dubai time)
      C: location (M4 or M5)

    Returns:
      { "success": true, "scanned_date": "DD/MM/YYYY HH:MM:SS AM/PM" }
      400 — missing/invalid code or location
      502 — Graph/Excel write failure
    """
    data = request.get_json(silent=True) or {}
    code     = str(data.get('code', '')).strip()
    location = str(data.get('location', '')).strip().upper()

    if not code:
        return jsonify({"success": False, "message": "Code is required."}), 400

    if location not in _IVT_VALID_LOCATIONS:
        return jsonify({"success": False,
                        "message": f"Invalid location '{location}'. Must be M4 or M5."}), 400

    # Server-side validation guard
    err = _ivt_validate_code(code)
    if err:
        _, message = err
        return jsonify({"success": False, "message": message}), 400

    try:
        from app.services.graph_client import append_row
        sheet = current_app.config['SHEET_IVT_LINE']
        scanned_date = _fmt_dubai(datetime.now(timezone.utc))
        append_row(sheet, [code, scanned_date, location], num_cols=3)
        logger.info("IVT_Line: appended code=%s date=%s location=%s", code, scanned_date, location)
        return jsonify({"success": True, "scanned_date": scanned_date}), 200

    except Exception as exc:
        logger.error("IVT enter failed for code %s: %s", code, exc)
        return jsonify({"success": False, "message": "Could not write to IVT_Line sheet. Try again."}), 502
