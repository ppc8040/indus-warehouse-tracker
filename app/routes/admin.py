"""
Admin dashboard — basic HTTP auth protected.

All data now served from PostgreSQL via movement_service / db_client.
graph_client is dormant and no longer referenced here.
"""
import logging
from functools import wraps
from flask import Blueprint, render_template, request, Response, jsonify, current_app
from app.services.movement_service import (
    get_recent_movements,
    get_all_pallet_statuses,
)
from app.services.db_client import ping as db_ping
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
    """Return ERROR-type movement rows (validation failures, system errors)."""
    limit = request.args.get('limit', 100, type=int)
    all_rows = get_recent_movements(limit * 5)  # fetch wider window to find errors
    errors = [
        r for r in all_rows
        if str(r.get('movement_type', '')).upper() == 'ERROR'
    ][:limit]
    return jsonify({"errors": errors})


@admin_bp.route('/api/pallet-statuses')
@_auth_required
def api_pallet_statuses():
    return jsonify({"statuses": get_all_pallet_statuses()})


@admin_bp.route('/api/stats')
@_auth_required
def api_stats():
    try:
        movements = get_recent_movements(500)
        statuses  = get_all_pallet_statuses()

        # BUG FIX: use lowercase snake_case keys returned by db_client,
        # not PascalCase keys from the old Excel/Graph era.
        in_c = sum(
            1 for s in statuses
            if str(s.get('current_status', '')).upper() == 'IN_WAREHOUSE'
        )
        out_c = sum(
            1 for s in statuses
            if str(s.get('current_status', '')).upper() == 'OUT_OF_WAREHOUSE'
        )
        overrides = sum(
            1 for m in movements
            if str(m.get('validation_status', '')).upper() == 'OVERRIDE'
        )
        errors = sum(
            1 for m in movements
            if str(m.get('movement_type', '')).upper() == 'ERROR'
        )

        return jsonify({
            "total_movements": len(movements),
            "total_errors": errors,
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
    """Test database connectivity. Returns db ping status."""
    ok = db_ping()
    return jsonify({"status": "ok" if ok else "error", "db": ok})
