"""
Admin dashboard — basic HTTP auth protected.
"""
import logging
from functools import wraps
from flask import Blueprint, render_template, request, Response, jsonify, current_app
from app.services.movement_service import (
    get_recent_movements, get_error_log, get_all_pallet_statuses,
)
from app.services.graph_client import test_connection
from app.utils.idempotency import pending_count

logger = logging.getLogger(__name__)
admin_bp = Blueprint('admin', __name__)


def _auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        user = current_app.config.get('ADMIN_USERNAME', 'admin')
        pw = current_app.config.get('ADMIN_PASSWORD', 'change-me')
        if not auth or auth.username != user or auth.password != pw:
            return Response('Admin login required.\n', 401,
                            {'WWW-Authenticate': 'Basic realm="Indus Admin"'})
        return f(*args, **kwargs)
    return decorated


@admin_bp.route('/')
@_auth_required
def dashboard():
    return render_template('admin/dashboard.html')


@admin_bp.route('/api/movements')
@_auth_required
def api_movements():
    limit = request.args.get('limit', 50, type=int)
    return jsonify({"movements": get_recent_movements(limit)})


@admin_bp.route('/api/errors')
@_auth_required
def api_errors():
    limit = request.args.get('limit', 100, type=int)
    return jsonify({"errors": get_error_log(limit)})


@admin_bp.route('/api/pallet-statuses')
@_auth_required
def api_pallet_statuses():
    return jsonify({"statuses": get_all_pallet_statuses()})


@admin_bp.route('/api/stats')
@_auth_required
def api_stats():
    try:
        movements = get_recent_movements(500)
        errors = get_error_log(500)
        statuses = get_all_pallet_statuses()
        in_c = sum(1 for s in statuses if str(s.get('Current_Status', '')).upper() == 'IN_WAREHOUSE')
        out_c = sum(1 for s in statuses if str(s.get('Current_Status', '')).upper() == 'OUT_OF_WAREHOUSE')
        overrides = sum(1 for m in movements if str(m.get('Validation_Status', '')).upper() == 'OVERRIDE')
        return jsonify({
            "total_movements": len(movements),
            "total_errors": len(errors),
            "pallets_in": in_c,
            "pallets_out": out_c,
            "override_count": overrides,
            "pending_keys": pending_count(),
        })
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return jsonify({"error": str(e)}), 500


@admin_bp.route('/api/connection-test')
@_auth_required
def api_conn_test():
    return jsonify(test_connection())
