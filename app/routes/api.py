"""
JSON API routes.

All data operations now go through db_client.py (PostgreSQL).
graph_client.py is no longer imported by any active route.

Security:
  PIN validation — /validate-pin and /movement (override path) validate
  the supervisor PIN against the configured SUPERVISOR_PIN.
  Both SAP-not-found overrides and movement-rejection overrides use the
  same direct numeric PIN validation. No tokens, no TTL, no replay risk.

  Duplicate guard — _check_duplicate() in movement_service.py applies to
  ALL movements including overrides. Returns status 'rejected_duplicate'
  so the frontend blocks cleanly without offering a supervisor override.

  Pallet_Status priority — /api/lookup-hu checks pallet_status (Postgres)
  first. If the HU is already recorded, stored Material and Qty are returned
  instead of SAP values (handles label vs SAP discrepancies).

  Offline sync — /sync is capped at 50 events per request.

  IVT Line — /api/ivt-check and /api/ivt-enter are completely isolated
  from the warehouse movement flow. They now read/write the ivt_scans
  Postgres table instead of the IVT_Line Excel worksheet.

  IVT duplicate guards (two layers):
    1. Full-code uniqueness  — the entire 33-digit code must not exist in
       ivt_scans (enforced by the UNIQUE constraint on the code column).
    2. Serial-level uniqueness — positions 17–22 (the 6-digit serial) must
       not exist in any previously stored code, regardless of part code or
       date segment. Prevents the same physical unit from being scanned
       twice under a different barcode variant.

  BDR Line — /api/bdr-check and /api/bdr-enter handle the BDR customer
  scanning flow. BDR codes are accepted in two formats:
    Piped:   BDR{9-digit-customer-code} | {part-number} | {YYDDD} | {6-char-serial} | T1
    Compact: BDR{9-digit-customer-code}{6-char-part}{5-digit-YYDDD}{6-char-serial}T1
  Both formats are normalised to the piped canonical form before storage.
  They are completely isolated from IVT and warehouse movement flows.
  BDR duplicate guards mirror IVT (full-code + serial-level).

  Destinations — /api/destinations returns the list of valid to_location
  values for the STOCK_OUT destination picker on the worker UI.
"""
import re
import logging
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, current_app
from app.services.movement_service import (
    process_movement,
    sync_offline_event,
    get_destinations,
)
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
#   Positions 23–32  (10 digits) : Customer part code — see sets below

_IVT_CUSTOMER_CODE    = '561469490'
_IVT_TOTAL_LENGTH     = 33
# Allowed at any valid location (M4 or M5)
_IVT_VALID_PART_CODES = {'8750722699', '8750722840'}
# Allowed ONLY when location is M5
_IVT_M5_ONLY_PART_CODES = {'8750741149', '8750751986'}
# Allowed ONLY when location is M4
_IVT_M4_ONLY_PART_CODES = {'8750782685'}
_IVT_VALID_LOCATIONS  = {'M4', 'M5'}


def _ivt_validate_code(code: str, location: str = ''):
    """
    Validate an IVT QR code string.
    Returns None if valid, or a (error_type, message) tuple if invalid.

    When location is empty (e.g. at /api/ivt-check before the worker has
    selected a location), M5-only and M4-only part codes pass structure
    validation and the location restriction is deferred to /api/ivt-enter
    where location is always present.

    Checks (in order):
      1. Non-numeric characters
      2. Total length must be 33
      3. Customer code segment (pos 0-8)
      4. Date segment (pos 9-16) — 00DDMMYY with valid DD/MM cross-validated
      5. Customer part code (pos 23-32):
           - Unrestricted codes: 8750722699, 8750722840 (any location)
           - M5-only codes:      8750741149, 8750751986 (location must be M5)
           - M4-only codes:      8750782685             (location must be M4)
             (location restrictions only enforced when location is non-empty)
    """
    bad = re.search(r'[^0-9]', code)
    if bad:
        ch = bad.group(0)
        return ('invalid_chars',
                f"Invalid character \u2018{ch}\u2019 found in code. "
                f"IVT codes must contain numbers only.")

    if len(code) != _IVT_TOTAL_LENGTH:
        return ('invalid_length',
                f"Code length is {len(code)} digits but must be exactly "
                f"{_IVT_TOTAL_LENGTH} digits.")

    customer = code[0:9]
    if customer != _IVT_CUSTOMER_CODE:
        return ('invalid_customer_code',
                f"Invalid customer code \u2018{customer}\u2019 "
                f"\u2014 expected {_IVT_CUSTOMER_CODE}.")

    date_seg = code[9:17]
    if date_seg[:2] != '00':
        return ('invalid_date',
                f"Invalid date segment \u2018{date_seg}\u2019 "
                f"\u2014 must start with 00 (format 00DDMMYY).")
    try:
        datetime.strptime(f"{date_seg[2:4]}{date_seg[4:6]}", "%d%m")
    except ValueError:
        return ('invalid_date',
                f"Invalid date segment \u2018{date_seg}\u2019 "
                f"\u2014 day {date_seg[2:4]} does not exist in month {date_seg[4:6]}.")

    part_code = code[23:33]

    if part_code in _IVT_VALID_PART_CODES:
        return None  # unrestricted — always valid

    if part_code in _IVT_M5_ONLY_PART_CODES:
        if not location:
            return None  # defer location check to /ivt-enter
        if location.upper() == 'M5':
            return None  # M5-only code at M5 — valid
        return ('invalid_part_code',
                f"Part code \u2018{part_code}\u2019 is only permitted at "
                f"location M5.")

    if part_code in _IVT_M4_ONLY_PART_CODES:
        if not location:
            return None  # defer location check to /ivt-enter
        if location.upper() == 'M4':
            return None  # M4-only code at M4 — valid
        return ('invalid_part_code',
                f"Part code \u2018{part_code}\u2019 is only permitted at "
                f"location M4.")

    all_valid = sorted(_IVT_VALID_PART_CODES | _IVT_M5_ONLY_PART_CODES | _IVT_M4_ONLY_PART_CODES)
    valid_list = ', '.join(all_valid[:-1]) + ' or ' + all_valid[-1]
    return ('invalid_part_code',
            f"Invalid customer part code \u2018{part_code}\u2019 "
            f"\u2014 must be {valid_list}.")


# ---------------------------------------------------------------------------
# BDR code validation
# ---------------------------------------------------------------------------
# Accepted formats (both parsed identically):
#
#   Piped (scanner output with separators):
#     BDR{CustomerCode} | {PartNumber} | {YYDDD} | {Serial} | T1
#
#   Compact (scanner output without separators, 31 chars total):
#     BDR{9-digit customer}{6-char part}{5-digit YYDDD}{6-char serial}T1
#     e.g. BDR7838200533353626200123456T1
#          ^^^         ^^^^^^     ^^^^^  ^^^^^^  ^^
#          BDR  customer(9) part(6) YYDDD(5) serial(6) T1(2)
#
#   CustomerCode  : 9 digits  — one of '783832005', '783832304'
#   PartNumber    : 6 alphanum — must be in _BDR_VALID_PARTS (e.g. '333536')
#   YYDDD         : 5 digits  — 2-digit year + 3-digit day-of-year (001-366)
#   Serial        : 6 alphanum — unique per part_number
#   T1            : literal suffix
#
# Valid locations: M4 or M5

_BDR_CUSTOMER_CODE   = 'BDR'           # prefix only — part numbers validated separately
_BDR_VALID_PART_NUMBERS = {'783832005', '783832304'}
_BDR_VALID_SUPPLIER  = '333536'
_BDR_VALID_LOCATIONS = {'M4', 'M5'}

# Piped format — tolerates any whitespace around pipe separators
_BDR_RE_PIPED = re.compile(
    r'^BDR(\d{9})\s*\|\s*(\w+)\s*\|\s*(\d{2})(\d{3})\s*\|\s*(\w{6})\s*\|\s*T1$',
    re.IGNORECASE
)

# Compact format — no separators, fixed field widths:
#   BDR + 9 + 6 + 2 + 3 + 6 + T1  =  3+9+6+2+3+6+2 = 31 chars
_BDR_RE_COMPACT = re.compile(
    r'^BDR(\d{9})(\w{6})(\d{2})(\d{3})(\w{6})T1$',
    re.IGNORECASE
)


def _bdr_parse(code: str):
    """
    Parse a BDR label string (piped or compact) into its components.
    Returns a dict with keys: customer_code, part_number, year, doy, serial
    Or returns None if neither format matches.
    """
    s = code.strip()

    m = _BDR_RE_PIPED.match(s)
    if m:
        return {
            'customer_code': m.group(1),
            'part_number':   m.group(2).upper(),
            'year':          m.group(3),
            'doy':           m.group(4),
            'serial':        m.group(5).upper(),
        }

    m = _BDR_RE_COMPACT.match(s)
    if m:
        return {
            'customer_code': m.group(1),
            'part_number':   m.group(2).upper(),
            'year':          m.group(3),
            'doy':           m.group(4),
            'serial':        m.group(5).upper(),
        }

    return None


def _bdr_validate_code(code: str, location: str = ''):
    """
    Validate a BDR label string (piped or compact format).
    Returns None if valid, or (error_type, message) tuple if invalid.
    """
    parsed = _bdr_parse(code)
    if parsed is None:
        return ('invalid_format',
                "Code does not match BDR label format. "
                "Expected piped: BDR{CustomerCode} | {PartNumber} | {YYDDD} | {Serial} | T1  "
                "or compact: BDR{9-digit customer}{6-char part}{YYDDD}{6-char serial}T1")

    if parsed['customer_code'] not in _BDR_VALID_PART_NUMBERS:
        return ('invalid_customer_code',
                f"Invalid BDR customer part number \u2018{parsed['customer_code']}\u2019 "
                f"\u2014 expected one of: {', '.join(sorted(_BDR_VALID_PART_NUMBERS))}.")
    
    if parsed['part_number'] != _BDR_VALID_SUPPLIER:
        return ('invalid_part_code',
                f"Invalid BDR supplier number \u2018{parsed['part_number']}\u2019 "
                f"\u2014 expected {_BDR_VALID_SUPPLIER}.")

    doy = int(parsed['doy'])
    if doy < 1 or doy > 366:
        return ('invalid_date',
                f"Invalid day-of-year \u2018{parsed['doy']}\u2019 "
                f"\u2014 must be between 001 and 366.")

    if location and location.upper() not in _BDR_VALID_LOCATIONS:
        return ('invalid_location',
                f"Invalid location \u2018{location}\u2019 "
                f"\u2014 BDR scanning is only permitted at M4 or M5.")

    return None


# ===========================================================================
# ROUTES
# ===========================================================================

@api_bp.route('/movement', methods=['POST'])
def record_movement():
    """
    Record a Stock IN or Stock OUT movement.

    For any supervisor override (is_override=True), supervisor_pin is
    validated directly as a numeric PIN against SUPERVISOR_PIN config.

    New field (STOCK_OUT only):
      to_location : destination for the pallet (e.g. 'Customer',
                    'Port Jebel Ali', 'Zone M2', etc.)

    Returns:
      200 — success
      409 — rejected_validation or rejected_duplicate
      502 — write failure
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
        to_location=data.get('to_location', ''),
    )

    if result.success:
        return jsonify(result.to_dict()), 200
    elif result.status in ('rejected_validation', 'rejected_duplicate',
                           'rejected_transition'):
        return jsonify(result.to_dict()), 409
    else:
        return jsonify(result.to_dict()), 502


@api_bp.route('/lookup-hu', methods=['POST'])
def lookup_hu():
    """
    Look up a Handling Unit.

    Checks pallet_status (Postgres) first. If the HU is already recorded,
    returns the stored Material and Qty instead of calling SAP.
    Only falls through to SAP when the HU is not yet in pallet_status.
    Returns HU_NOT_FOUND if SAP also has no record.

    Also returns the pallet's current location and status so the worker
    UI can pre-fill the STOCK_OUT destination card.
    """
    data = request.get_json(silent=True) or {}
    hu_raw = data.get('handling_unit', '')

    ok, hu = validate_handling_unit(hu_raw)
    if not ok:
        return jsonify({"success": False, "error_code": "INVALID_HU",
                        "message": hu}), 400

    # ── Check pallet_status in Postgres first ──
    try:
        from app.services.db_client import get_pallet_status
        existing = get_pallet_status(hu)
        if existing:
            stored_part = str(existing.get('indus_part', '') or '').strip().upper()
            stored_qty  = str(existing.get('pallet_qty', '') or '').strip()
            if stored_qty.endswith('.0') and stored_qty[:-2].isdigit():
                stored_qty = stored_qty[:-2]
            if stored_part and stored_qty and stored_qty != '0':
                logger.info(
                    "HU %s found in pallet_status (Postgres) — "
                    "using stored values (part=%s qty=%s)",
                    hu, stored_part, stored_qty,
                )
                return jsonify({
                    "success": True,
                    "handling_unit": hu,
                    "indus_part": stored_part,
                    "pallet_qty": stored_qty,
                    "current_status": existing.get('current_status', ''),
                    "last_location": existing.get('last_location', ''),
                    "last_movement_type": existing.get('last_movement_type', ''),
                    "hu_batch": existing.get('hu_batch') or '',
                    "hu_creation_date": str(existing.get('hu_creation_date_dubai') or ''),
                    "source": "pallet_status",
                }), 200
    except Exception as ps_exc:
        # Non-fatal — fall through to SAP
        logger.warning(
            "pallet_status Postgres check failed (non-fatal), "
            "falling back to SAP: %s", ps_exc
        )

    # ── SAP lookup (fallback when HU not yet in pallet_status) ──
    try:
        from app.services.sap_client import lookup_hu as sap_lookup
        result = sap_lookup(hu)
        return jsonify({
            "success": True,
            "handling_unit": result["handling_unit"],
            "indus_part": result["indus_part"],
            "pallet_qty": result["pallet_qty"],
            "current_status": "",
            "last_location": "",
            "last_movement_type": "",
            "hu_batch": result.get("hu_batch") or "",
            "hu_creation_date": result.get("hu_creation_date") or "",  # ← was hardcoded ""
            "source": "sap",
        }), 200
    except Exception as exc:
        error_code = getattr(exc, 'error_code', 'SAP_UNKNOWN')
        msg = str(exc)
        logger.error("HU lookup failed for %s [%s]: %s", hu, error_code, msg)
        return jsonify({"success": False, "error_code": error_code,
                        "message": msg}), 502


@api_bp.route('/destinations', methods=['GET'])
def get_destination_list():
    """
    Return the list of valid to_location values for the STOCK_OUT
    destination picker on the worker UI.

    Returns:
      { "destinations": ["Customer", "Port Jebel Ali", ...] }
    """
    return jsonify({"destinations": get_destinations()}), 200


@api_bp.route('/sync', methods=['POST'])
def sync_offline():
    """Sync offline-queued movements. Capped at 50 events per request."""
    data = request.get_json(silent=True) or {}
    events = data.get('events', [])

    if not events:
        return jsonify({"success": True, "message": "No events to sync.",
                        "results": []}), 200

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
            logger.error("Sync event failed: %s", e)
            results.append({
                "idempotency_key": ev.get('idempotency_key', ''),
                "handling_unit": ev.get('handling_unit', ''),
                "success": False,
                "status": "failed_write_logged",
                "message": str(e),
            })

    return jsonify({"success": True,
                    "message": f"Processed {len(results)} events.",
                    "results": results}), 200


@api_bp.route('/validate-pin', methods=['POST'])
def validate_pin():
    """
    Validate supervisor PIN.
    Returns valid:true on success.
    """
    data = request.get_json(silent=True) or {}
    pin     = data.get('pin', '')
    correct = current_app.config.get('SUPERVISOR_PIN', '9999')

    if validate_supervisor_pin(pin, correct):
        return jsonify({"valid": True}), 200
    return jsonify({"valid": False, "message": "Invalid PIN."}), 403


# ===========================================================================
# IVT LINE ROUTES
# Now backed by the ivt_scans Postgres table.
# Schema: id (serial), code (text unique), scanned_date (timestamptz), location (text)
#
# Duplicate guards (applied in order):
#   1. Full-code check  — entire 33-digit code must not already exist
#   2. Serial check     — 6-digit serial (pos 17–22) must not exist in any
#      stored code, regardless of part code or date segment
# ===========================================================================

@api_bp.route('/ivt-check', methods=['POST'])
def ivt_check():
    """
    Validate and duplicate-check an IVT QR code.

    Called immediately after scan, BEFORE the worker selects a location.
    Location is therefore NOT used for validation here — the M5-only and
    M4-only part code restrictions are enforced only at /api/ivt-enter
    once the worker has confirmed their location.

    Validation rules (server-side mirror of the client-side checks):
      1. No non-numeric characters
      2. Total length must be exactly 33 digits
      3. Positions 0-8  must equal '561469490'  (customer code)
      4. Positions 9-16 must be valid 00DDMMYY   (date segment)
      5. Positions 23-32 must be a known part code (structure check only)

    Duplicate checks (in order):
      A. Full-code match  → duplicate_type: "code"
      B. Serial match     → duplicate_type: "serial"

    Returns:
      { "valid": false, "error_type": "...", "message": "..." }
      { "valid": true, "duplicate": false }
      { "valid": true, "duplicate": true, "duplicate_type": "code"|"serial",
        "scanned_date": "..." }
    """
    data = request.get_json(silent=True) or {}
    code = str(data.get('code', '')).strip()

    if not code:
        return jsonify({"success": False, "message": "Code is required."}), 400

    # Validate structure only — no location passed, location restriction deferred
    err = _ivt_validate_code(code)
    if err:
        error_type, message = err
        logger.warning("IVT validation failed for code %r [%s]: %s",
                       code, error_type, message)
        return jsonify({"valid": False, "error_type": error_type,
                        "message": message}), 200

    try:
        from app.services.db_client import ivt_find, ivt_find_by_serial

        # ── 1. Full-code duplicate check ──
        existing = ivt_find(code)
        if existing:
            return jsonify({"valid": True, "duplicate": True,
                            "duplicate_type": "code",
                            "scanned_date": existing.get('scanned_date', '')}), 200

        # ── 2. Serial-level duplicate check ──
        serial   = code[17:23]
        part_code = code[23:33]
        serial_hit = ivt_find_by_serial(serial, part_code)
        if serial_hit:
            logger.warning(
                "IVT serial duplicate: serial=%s new_code=%s existing_code=%s",
                serial, code, serial_hit.get('code', '')
            )
            return jsonify({"valid": True, "duplicate": True,
                            "duplicate_type": "serial",
                            "scanned_date": serial_hit.get('scanned_date', '')}), 200

        return jsonify({"valid": True, "duplicate": False}), 200

    except Exception as exc:
        logger.error("IVT check (Postgres) failed for code %s: %s", code, exc)
        return jsonify({"success": False,
                        "message": "Could not check IVT database. Try again."}), 502


@api_bp.route('/ivt-enter', methods=['POST'])
def ivt_enter():
    """
    Append a validated, unique IVT code to the ivt_scans table.

    Location is required and fully validated here, including the M5-only
    restriction for part codes 8750741149 and 8750751986, and the M4-only
    restriction for part code 8750782685.

    Uniqueness is enforced at two levels immediately before the write:
      1. Full-code check  — entire 33-digit code must not already exist
      2. Serial check     — 6-digit serial (pos 17–22) must not exist in
         any previously stored code

    Returns:
      { "success": true, "scanned_date": "DD/MM/YYYY HH:MM:SS AM/PM" }
    """
    data = request.get_json(silent=True) or {}
    code     = str(data.get('code', '')).strip()
    location = str(data.get('location', '')).strip().upper()

    if not code:
        return jsonify({"success": False, "message": "Code is required."}), 400

    if location not in _IVT_VALID_LOCATIONS:
        return jsonify({"success": False,
                        "message": f"Invalid location '{location}'. "
                                   f"Must be M4 or M5."}), 400

    # Full validation including location restrictions
    err = _ivt_validate_code(code, location)
    if err:
        _, message = err
        return jsonify({"success": False, "message": message}), 400

    try:
        from app.services.db_client import ivt_find, ivt_find_by_serial, ivt_insert

        # ── 1. Full-code uniqueness check ──
        if ivt_find(code):
            return jsonify({"success": False,
                            "message": "This code was already scanned."}), 409

        # ── 2. Serial-level uniqueness check ──
        serial    = code[17:23]
        part_code = code[23:33]
        serial_hit = ivt_find_by_serial(serial, part_code)
        if serial_hit:
            logger.warning(
                "IVT serial duplicate blocked on enter: serial=%s new_code=%s "
                "existing_code=%s",
                serial, code, serial_hit.get('code', '')
            )
            return jsonify({"success": False,
                            "message": f"Serial number '{serial}' has already "
                                       f"been scanned (scanned on "
                                       f"{serial_hit.get('scanned_date', 'unknown')}"
                                       f" at {serial_hit.get('location', 'unknown')}"
                                       f")."}), 409

        scanned_date = datetime.now(timezone.utc)
        ivt_insert(code, scanned_date, location)
        logger.info("IVT inserted (Postgres): code=%s location=%s", code, location)
        return jsonify({"success": True, "scanned_date": scanned_date}), 200

    except Exception as exc:
        logger.error("IVT enter (Postgres) failed for code %s: %s", code, exc)
        return jsonify({"success": False,
                        "message": "Could not write to IVT database. Try again."}), 502


# ===========================================================================
# BDR LINE ROUTES
# Backed by the bdr_scans Postgres table.
# Schema: id (serial), code (text unique), customer_code (text),
#         part_number (text), year_2d (text), doy (text), serial (text),
#         scanned_date (timestamptz), location (text)
#
# Duplicate guards (applied in order):
#   1. Full-code check  — entire code string must not already exist
#   2. Serial check     — 6-char serial must not exist for the same part_number
# ===========================================================================

@api_bp.route('/bdr-check', methods=['POST'])
def bdr_check():
    """
    Validate and duplicate-check a BDR label string.

    Called immediately after scan, BEFORE the worker selects a location.
    Accepts both piped and compact (no-separator) barcode formats.

    Validation:
      1. Format must match piped or compact BDR regex
      2. Customer code must equal '783832304'
      3. Part number must be in _BDR_VALID_PARTS
      4. Day-of-year must be 001-366

    Duplicate checks (in order):
      A. Full-code match  → duplicate_type: "code"
      B. Serial match     → duplicate_type: "serial"

    Returns:
      { "valid": false, "error_type": "...", "message": "..." }
      { "valid": true, "duplicate": false, "parsed": {...} }
      { "valid": true, "duplicate": true, "duplicate_type": "code"|"serial",
        "scanned_date": "...", "parsed": {...} }
    """
    data = request.get_json(silent=True) or {}
    code = str(data.get('code', '')).strip()

    if not code:
        return jsonify({"success": False, "message": "Code is required."}), 400

    err = _bdr_validate_code(code)
    if err:
        error_type, message = err
        logger.warning("BDR validation failed for code %r [%s]: %s",
                       code, error_type, message)
        return jsonify({"valid": False, "error_type": error_type,
                        "message": message}), 200

    parsed = _bdr_parse(code)

    try:
        from app.services.db_client import bdr_find, bdr_find_by_serial

        # ── 1. Full-code duplicate check ──
        existing = bdr_find(code)
        if existing:
            return jsonify({"valid": True, "duplicate": True,
                            "duplicate_type": "code",
                            "scanned_date": existing.get('scanned_date', ''),
                            "parsed": parsed}), 200

        # ── 2. Serial-level duplicate check ──
        serial_hit = bdr_find_by_serial(parsed['serial'], parsed['part_number'])
        if serial_hit:
            logger.warning(
                "BDR serial duplicate: serial=%s part=%s existing_code=%s",
                parsed['serial'], parsed['part_number'], serial_hit.get('code', '')
            )
            return jsonify({"valid": True, "duplicate": True,
                            "duplicate_type": "serial",
                            "scanned_date": serial_hit.get('scanned_date', ''),
                            "parsed": parsed}), 200

        return jsonify({"valid": True, "duplicate": False, "parsed": parsed}), 200

    except Exception as exc:
        logger.error("BDR check (Postgres) failed for code %s: %s", code, exc)
        return jsonify({"success": False,
                        "message": "Could not check BDR database. Try again."}), 502


@api_bp.route('/bdr-enter', methods=['POST'])
def bdr_enter():
    """
    Append a validated, unique BDR code to the bdr_scans table.

    Location is required. Valid locations: M4, M5.
    Accepts both piped and compact (no-separator) barcode formats.

    Uniqueness enforced at two levels immediately before the write:
      1. Full-code check  — entire code must not already exist
      2. Serial check     — 6-char serial must not exist for the same part_number

    Returns:
      { "success": true, "scanned_date": "DD/MM/YYYY HH:MM:SS AM/PM", "parsed": {...} }
    """
    data = request.get_json(silent=True) or {}
    code     = str(data.get('code', '')).strip()
    location = str(data.get('location', '')).strip().upper()

    if not code:
        return jsonify({"success": False, "message": "Code is required."}), 400

    if location not in _BDR_VALID_LOCATIONS:
        return jsonify({"success": False,
                        "message": f"Invalid location '{location}'. "
                                   f"BDR scanning is only permitted at M4 or M5."}), 400

    err = _bdr_validate_code(code, location)
    if err:
        _, message = err
        return jsonify({"success": False, "message": message}), 400

    parsed = _bdr_parse(code)

    try:
        from app.services.db_client import bdr_find, bdr_find_by_serial, bdr_insert

        # ── 1. Full-code uniqueness check ──
        if bdr_find(code):
            return jsonify({"success": False,
                            "message": "This BDR code was already scanned."}), 409

        # ── 2. Serial-level uniqueness check ──
        serial_hit = bdr_find_by_serial(parsed['serial'], parsed['part_number'])
        if serial_hit:
            logger.warning(
                "BDR serial duplicate blocked on enter: serial=%s part=%s "
                "existing_code=%s",
                parsed['serial'], parsed['part_number'], serial_hit.get('code', '')
            )
            return jsonify({"success": False,
                            "message": f"Serial '{parsed['serial']}' for part "
                                       f"{parsed['part_number']} has already been scanned "
                                       f"(on {serial_hit.get('scanned_date', 'unknown')} "
                                       f"at {serial_hit.get('location', 'unknown')})."}), 409

        scanned_date = _fmt_dubai(datetime.now(timezone.utc))
        bdr_insert(
            code=code,
            customer_code=parsed['customer_code'],
            part_number=parsed['part_number'],
            year_2d=parsed['year'],
            doy=parsed['doy'],
            serial=parsed['serial'],
            scanned_date=scanned_date,
            location=location,
        )
        logger.info("BDR inserted (Postgres): code=%s part=%s location=%s",
                    code, parsed['part_number'], location)
        return jsonify({"success": True, "scanned_date": scanned_date,
                        "parsed": parsed}), 200

    except Exception as exc:
        logger.error("BDR enter (Postgres) failed for code %s: %s", code, exc)
        return jsonify({"success": False,
                        "message": "Could not write to BDR database. Try again."}), 502
