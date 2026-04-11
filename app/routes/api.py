"""
JSON API routes.
"""
import logging
from flask import Blueprint, request, jsonify, current_app
from app.services.movement_service import process_movement, sync_offline_event
from app.utils.validators import validate_supervisor_pin, validate_handling_unit

logger = logging.getLogger(__name__)
api_bp = Blueprint('api', __name__)


@api_bp.route('/movement', methods=['POST'])
def record_movement():
    """
    Record a Stock IN or Stock OUT movement.
    indus_part and pallet_qty now come from the SAP HU lookup (done on the frontend
    before this call). customer_part is intentionally left blank.
    POST body: {
        "handling_unit": "100000000000839157",
        "movement_type": "STOCK_IN",
        "indus_part": "CCR2324",
        "customer_part": "",
        "pallet_qty": "39",
        "job_no": "",
        "operator_id": "WORKER",
        "device_id": "DEV-ABC123",
        "location": "M4",
        ...
    }
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
    elif result.status == 'rejected_validation':
        return jsonify(result.to_dict()), 409
    else:
        return jsonify(result.to_dict()), 502


@api_bp.route('/lookup-hu', methods=['POST'])
def lookup_hu():
    """
    Look up a Handling Unit in SAP S/4HANA and return its material and quantity.

    POST body: { "handling_unit": "100000000000839157" }

    Response (success):
    {
        "success": true,
        "handling_unit": "100000000000839157",
        "indus_part": "AJM0488",
        "pallet_qty": "36"
    }

    Response (failure):
    {
        "success": false,
        "message": "Handling Unit not found in SAP."
    }
    """
    data = request.get_json(silent=True) or {}
    hu_raw = data.get('handling_unit', '')

    # Validate HU format before hitting SAP
    ok, hu = validate_handling_unit(hu_raw)
    if not ok:
        return jsonify({"success": False, "message": hu}), 400

    try:
        from app.services.sap_client import lookup_hu as sap_lookup, SAPLookupError
        result = sap_lookup(hu)
        return jsonify({
            "success": True,
            "handling_unit": result["handling_unit"],
            "indus_part": result["indus_part"],
            "pallet_qty": result["pallet_qty"],
        }), 200
    except Exception as e:
        # SAPLookupError and any unexpected exception both surface here
        msg = str(e)
        logger.error("HU lookup failed for %s: %s", hu, msg)
        return jsonify({"success": False, "message": msg}), 502


@api_bp.route('/sync', methods=['POST'])
def sync_offline():
    data = request.get_json(silent=True) or {}
    events = data.get('events', [])
    if not events:
        return jsonify({"success": True, "message": "No events to sync.", "results": []}), 200

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

    return jsonify({"success": True, "message": f"Processed {len(results)} events.", "results": results}), 200


@api_bp.route('/validate-pin', methods=['POST'])
def validate_pin():
    data = request.get_json(silent=True) or {}
    pin = data.get('pin', '')
    correct = current_app.config.get('SUPERVISOR_PIN', '9999')
    if validate_supervisor_pin(pin, correct):
        return jsonify({"valid": True}), 200
    return jsonify({"valid": False, "message": "Invalid PIN."}), 403
