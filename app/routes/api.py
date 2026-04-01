"""
JSON API routes.
"""
import logging
from flask import Blueprint, request, jsonify, current_app
from app.services.movement_service import (
    lookup_pallet, process_movement, sync_offline_event,
)
from app.utils.validators import validate_supervisor_pin

logger = logging.getLogger(__name__)
api_bp = Blueprint('api', __name__)


@api_bp.route('/lookup', methods=['POST'])
def pallet_lookup():
    data = request.get_json(silent=True) or {}
    hu = data.get('handling_unit', '')
    if not hu:
        return jsonify({"success": False, "status": "rejected_validation",
                        "message": "Scan a barcode first."}), 400
    result = lookup_pallet(hu)
    code = 200 if result.success else (400 if result.status == 'rejected_validation' else 502)
    return jsonify(result.to_dict()), code


@api_bp.route('/movement', methods=['POST'])
def record_movement():
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
        operator_id=data.get('operator_id', 'WORKER'),
        device_id=data.get('device_id', ''),
        notes=data.get('notes', ''),
        is_override=is_override,
        override_reason=data.get('override_reason', ''),
        client_timestamp=data.get('timestamp', ''),
        idempotency_key=data.get('idempotency_key', ''),
    )

    if result.success:
        return jsonify(result.to_dict()), 200
    elif result.status == 'rejected_validation':
        return jsonify(result.to_dict()), 409
    else:
        return jsonify(result.to_dict()), 502


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
