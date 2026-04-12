"""
JSON API routes.

Security additions in this revision:

  Fix 1 — PIN brute-force protection
    Both /validate-pin and /movement (override path) share an in-memory
    per-IP failure counter.  After 3 wrong PINs the IP is locked for 30 s.
    Counter resets on a correct PIN.

  Fix 2 — Signed one-use override token for SAP-not-found path
    /validate-pin now returns an override_token (32-char hex) on success.
    The token is stored server-side with a 10-minute TTL and consumed
    (single-use) when the movement POST arrives.  Manually setting
    _isNotFoundOverride=true in a browser console is useless without
    a valid token.

  Fix 3 — Duplicate check fails closed
    _check_duplicate_in_log() now returns a friendly retry message on
    Excel read error instead of silently allowing the movement through.

  Fix 5 — Device name registration
    New /register-device route.  First visit shows a name-entry overlay;
    the chosen name is stored in Device_Names sheet (Device_ID, Device_Name,
    Registered_Time) and checked for uniqueness before accepting.

  Fix 6 — /sync payload capped at 50 events
"""
import logging
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, current_app
from app.services.movement_service import process_movement, sync_offline_event
from app.utils.validators import validate_supervisor_pin, validate_handling_unit

logger = logging.getLogger(__name__)
api_bp = Blueprint('api', __name__)

# ---------------------------------------------------------------------------
# Fix 1 — in-memory PIN rate limiter
#   Structure:  { ip: {"failures": [timestamp, ...], "locked_until": float} }
#   3 failures within the rolling window → 30-second lockout.
#   A correct PIN clears the record for that IP immediately.
# ---------------------------------------------------------------------------
_PIN_MAX_FAILURES  = 3
_PIN_LOCKOUT_SECS  = 30
_pin_failures: dict = defaultdict(lambda: {"failures": [], "locked_until": 0.0})


def _get_client_ip() -> str:
    """Best-effort client IP (works behind Heroku's proxy)."""
    return (request.headers.get('X-Forwarded-For', '') or
            request.remote_addr or 'unknown').split(',')[0].strip()


def _pin_is_locked(ip: str) -> bool:
    """Return True if the IP is currently in a PIN lockout period."""
    rec = _pin_failures[ip]
    if time.time() < rec["locked_until"]:
        return True
    # Prune expired lockout
    rec["locked_until"] = 0.0
    return False


def _pin_record_failure(ip: str) -> bool:
    """
    Record a failed PIN attempt.  Lock the IP if the threshold is reached.
    Returns True if the IP is now locked.
    """
    now = time.time()
    rec = _pin_failures[ip]
    # Keep only failures within the last lockout window
    rec["failures"] = [t for t in rec["failures"] if now - t < _PIN_LOCKOUT_SECS]
    rec["failures"].append(now)
    if len(rec["failures"]) >= _PIN_MAX_FAILURES:
        rec["locked_until"] = now + _PIN_LOCKOUT_SECS
        rec["failures"] = []
        return True
    return False


def _pin_clear(ip: str):
    """Clear failure record for an IP after a successful PIN."""
    _pin_failures[ip] = {"failures": [], "locked_until": 0.0}


# ---------------------------------------------------------------------------
# Fix 2 — single-use override token store
#   { token_hex: {"issued_at": float, "used": bool} }
# ---------------------------------------------------------------------------
_TOKEN_TTL_SECS = 600  # 10 minutes
_override_tokens: dict = {}


def _issue_override_token() -> str:
    """Generate and store a one-use 32-char hex override token."""
    # Prune expired tokens to avoid unbounded growth
    now = time.time()
    expired = [k for k, v in _override_tokens.items()
               if now - v["issued_at"] > _TOKEN_TTL_SECS]
    for k in expired:
        del _override_tokens[k]

    token = secrets.token_hex(16)  # 32 hex chars
    _override_tokens[token] = {"issued_at": now, "used": False}
    return token


def _consume_override_token(token: str) -> bool:
    """
    Validate and consume a token.
    Returns True only if token exists, is within TTL, and has not been used.
    """
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
    supervisor_pin is the short numeric PIN — validated normally.
    The two cases are distinguished by token length: tokens are 32 hex chars,
    numeric PINs are always shorter.
    """
    data = request.get_json(silent=True) or {}
    hu = data.get('handling_unit', '')
    mt = data.get('movement_type', '')

    if not hu or not mt:
        return jsonify({"success": False, "status": "rejected_validation",
                        "message": "Handling Unit and movement type are required."}), 400

    is_override = data.get('is_override', False)
    if is_override:
        ip  = _get_client_ip()
        pin = data.get('supervisor_pin', '')

        # Distinguish token (32 hex) vs numeric PIN
        if len(pin) == 32 and all(c in '0123456789abcdefABCDEF' for c in pin):
            # SAP-not-found override — validate one-use token
            if not _consume_override_token(pin):
                return jsonify({"success": False, "status": "rejected_validation",
                                "message": "Override authorisation expired or already used. "
                                           "Supervisor must re-enter PIN."}), 403
        else:
            # Movement-rejection override — validate numeric PIN with rate limiter
            if _pin_is_locked(ip):
                return jsonify({"success": False, "status": "rejected_validation",
                                "message": "Too many failed attempts. Wait 30 seconds."}), 429
            correct = current_app.config.get('SUPERVISOR_PIN', '9999')
            if not validate_supervisor_pin(pin, correct):
                locked = _pin_record_failure(ip)
                msg = ("Too many failed attempts. Wait 30 seconds."
                       if locked else "Invalid supervisor PIN.")
                return jsonify({"success": False, "status": "rejected_validation",
                                "message": msg}), 403
            _pin_clear(ip)

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
    elif result.status == 'rejected_validation':
        return jsonify(result.to_dict()), 409
    else:
        return jsonify(result.to_dict()), 502


@api_bp.route('/lookup-hu', methods=['POST'])
def lookup_hu():
    """
    Look up a Handling Unit in SAP S/4HANA.
    Returns material and quantity, or HU_NOT_FOUND if the HU is not in SAP.
    """
    data = request.get_json(silent=True) or {}
    hu_raw = data.get('handling_unit', '')

    ok, hu = validate_handling_unit(hu_raw)
    if not ok:
        return jsonify({"success": False, "error_code": "INVALID_HU", "message": hu}), 400

    try:
        from app.services.sap_client import lookup_hu as sap_lookup
        result = sap_lookup(hu)
        return jsonify({
            "success": True,
            "handling_unit": result["handling_unit"],
            "indus_part": result["indus_part"],
            "pallet_qty": result["pallet_qty"],
        }), 200
    except Exception as exc:
        error_code = getattr(exc, 'error_code', 'SAP_UNKNOWN')
        msg = str(exc)
        logger.error("HU lookup failed for %s [%s]: %s", hu, error_code, msg)
        return jsonify({"success": False, "error_code": error_code, "message": msg}), 502


@api_bp.route('/sync', methods=['POST'])
def sync_offline():
    """Sync offline-queued movements.  Capped at 50 events per request."""
    data = request.get_json(silent=True) or {}
    events = data.get('events', [])

    if not events:
        return jsonify({"success": True, "message": "No events to sync.", "results": []}), 200

    # Fix 6 — reject oversized batches
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
    Fix 1: rate-limited per IP (3 failures → 30-second lockout).
    Fix 2: returns a one-use override_token on success for SAP-not-found overrides.
    """
    ip = _get_client_ip()

    if _pin_is_locked(ip):
        return jsonify({"valid": False,
                        "message": "Too many failed attempts. Wait 30 seconds."}), 429

    data = request.get_json(silent=True) or {}
    pin  = data.get('pin', '')
    correct = current_app.config.get('SUPERVISOR_PIN', '9999')

    if validate_supervisor_pin(pin, correct):
        _pin_clear(ip)
        token = _issue_override_token()
        return jsonify({"valid": True, "override_token": token}), 200

    locked = _pin_record_failure(ip)
    msg = ("Too many failed attempts. Wait 30 seconds."
           if locked else "Invalid PIN.")
    return jsonify({"valid": False, "message": msg}), 403


@api_bp.route('/register-device', methods=['POST'])
def register_device():
    """
    Fix 5 — Device name registration.

    POST body: { "device_id": "DEV-XXXX", "device_name": "Ahmed" }

    Checks Device_Names sheet for:
      a) device_id already registered — returns that name (re-registration allowed
         on same device, e.g. after browser data cleared).
      b) device_name already taken by a DIFFERENT device_id — returns {taken: true}.
      c) Neither — writes a new row and returns {success: true}.
    """
    data = request.get_json(silent=True) or {}
    device_id   = str(data.get('device_id', '')).strip()
    device_name = str(data.get('device_name', '')).strip()

    if not device_id or not device_name:
        return jsonify({"success": False,
                        "message": "device_id and device_name are required."}), 400

    if len(device_name) > 40:
        return jsonify({"success": False,
                        "message": "Name is too long. Keep it under 40 characters."}), 400

    try:
        from app.services.graph_client import read_all_rows_as_dicts, append_row
        sheet = current_app.config['SHEET_DEVICE_NAMES']
        rows  = read_all_rows_as_dicts(sheet)

        norm_name = device_name.strip().upper()

        for row in rows:
            existing_id   = str(row.get('Device_ID',   '')).strip()
            existing_name = str(row.get('Device_Name', '')).strip().upper()

            # Same device re-registering (e.g. localStorage cleared)
            if existing_id == device_id:
                # Return the stored name so the frontend can restore it
                return jsonify({
                    "success": True,
                    "already_registered": True,
                    "device_name": str(row.get('Device_Name', device_name)).strip(),
                }), 200

            # Name taken by a different device
            if existing_name == norm_name:
                return jsonify({
                    "success": False,
                    "taken": True,
                    "message": f"'{device_name}' is already in use on another scanner. "
                               "Please choose a different name.",
                }), 409

        # New device + unique name — write to sheet
        now_utc   = datetime.now(timezone.utc)
        reg_time  = _fmt_dubai(now_utc)
        append_row(sheet, [device_id, device_name, reg_time], 3)

        logger.info("Device registered: id=%s name=%s", device_id, device_name)
        return jsonify({"success": True, "already_registered": False,
                        "device_name": device_name}), 200

    except Exception as e:
        logger.error("register-device failed: %s", e)
        return jsonify({"success": False,
                        "message": "Could not reach the system. Try again."}), 502
