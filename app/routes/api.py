"""
JSON API routes.

Security:
  PIN validation — /validate-pin and /movement (override path) validate
  the supervisor PIN against the configured SUPERVISOR_PIN.
  For SAP-not-found overrides, /validate-pin returns a signed one-use
  override token (32-char hex, 10-minute TTL) which must be sent with
  the movement POST. Single-use prevents token replay.

  Duplicate guard — _check_duplicate_in_log() applies to ALL movements
  including overrides. Returns status 'rejected_duplicate' so the
  frontend blocks cleanly without offering a supervisor override option.

  Pallet_Status priority — /api/lookup-hu checks Pallet_Status first.
  If the HU is already recorded, stored Material and Qty are returned
  instead of SAP values (handles label vs SAP discrepancies).

  Offline sync — /sync is capped at 50 events per request.
"""
import logging
import secrets
import time
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, current_app
from app.services.movement_service import process_movement, sync_offline_event
from app.utils.validators import validate_supervisor_pin, validate_handling_unit

logger = logging.getLogger(__name__)
api_bp = Blueprint('api', __name__)


# ---------------------------------------------------------------------------
# Single-use override token store
# ---------------------------------------------------------------------------
_TOKEN_TTL_SECS = 600
_override_tokens: dict = {}


def _issue_override_token() -> str:
    now = time.time()
    expired = [k for k, v in _override_tokens.items()
               if now - v["issued_at"] > _TOKEN_TTL_SECS]
    for k in expired:
        del _override_tokens[k]
    token = secrets.token_hex(16)
    _override_tokens[token] = {"issued_at": now, "used": False}
    return token


def _consume_override_token(token: str) -> bool:
    rec = _override_tokens.get(token)
    if not rec:
        return False
    if rec["used"]:
        return False
    if time.time() - rec["issued_at"] > _TOKEN_TTL_SECS:
        del _override_tokens[token]
        return False
    rec["used"] = True
    return True


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


# ===========================================================================
# ROUTES
# ===========================================================================

@api_bp.route('/movement', methods=['POST'])
def record_movement():
    """
    Record a Stock IN or Stock OUT movement.

    For SAP-not-found supervisor overrides (is_override=True from the NFO modal),
    supervisor_pin must be a valid one-use override token issued by /validate-pin.

    For movement-rejection overrides (is_override=True from the rejection modal),
    supervisor_pin is the short numeric PIN validated directly.
    The two cases are distinguished by token length: tokens are 32 hex chars,
    numeric PINs are always shorter.

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

        if len(pin) == 32 and all(c in '0123456789abcdefABCDEF' for c in pin):
            # SAP-not-found override — validate one-use token
            if not _consume_override_token(pin):
                return jsonify({"success": False, "status": "rejected_validation",
                                "message": "Override authorisation expired or already used. "
                                           "Supervisor must re-enter PIN."}), 403
        else:
            # Movement-rejection override — validate numeric PIN directly
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
    Returns a one-use override_token on success for SAP-not-found overrides.
    """
    data = request.get_json(silent=True) or {}
    pin  = data.get('pin', '')
    correct = current_app.config.get('SUPERVISOR_PIN', '9999')

    if validate_supervisor_pin(pin, correct):
        token = _issue_override_token()
        return jsonify({"valid": True, "override_token": token}), 200

    return jsonify({"valid": False, "message": "Invalid PIN."}), 403
