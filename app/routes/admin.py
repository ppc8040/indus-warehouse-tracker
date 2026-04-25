"""
Admin dashboard — basic HTTP auth protected.

All data served from PostgreSQL via movement_service / db_client.
"""
import logging
from functools import wraps
from flask import Blueprint, render_template, request, Response, jsonify, current_app
from app.services.db_client import (
    get_recent_movements,
    get_all_pallet_statuses,
    ping as db_ping,
)
from app.services.db_client import _get_conn   # analytics queries
from app.utils.idempotency import pending_count

logger = logging.getLogger(__name__)
admin_bp = Blueprint('admin', __name__)


def _auth_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        user = current_app.config.get('ADMIN_USERNAME', 'admin')
        pw   = current_app.config.get('ADMIN_PASSWORD', 'change-me')
        if not auth or auth.username != user or auth.password != pw:
            return Response('Admin login required.\n', 401,
                            {'WWW-Authenticate': 'Basic realm="Indus Admin"'})
        return f(*args, **kwargs)
    return decorated


@admin_bp.route('/')
@_auth_required
def dashboard():
    return render_template('admin/dashboard.html')


# ── existing data endpoints (field names now snake_case, JS updated) ──────────

@admin_bp.route('/api/movements')
@_auth_required
def api_movements():
    limit = request.args.get('limit', 100, type=int)
    return jsonify({"movements": get_recent_movements(limit)})


@admin_bp.route('/api/pallet-statuses')
@_auth_required
def api_pallet_statuses():
    return jsonify({"statuses": get_all_pallet_statuses()})


@admin_bp.route('/api/errors')
@_auth_required
def api_errors():
    """Return ERROR-type movement rows from the movements table."""
    limit = request.args.get('limit', 100, type=int)
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, event_id,
                           timestamp_utc AT TIME ZONE 'Asia/Dubai' AS timestamp_dubai,
                           handling_unit, movement_type, validation_status,
                           operator_id, device_id, location, sync_status
                    FROM movements
                    WHERE movement_type = 'ERROR'
                       OR validation_status NOT IN ('VALID', 'OVERRIDE')
                    ORDER BY timestamp_utc DESC
                    LIMIT %(limit)s
                """, {"limit": limit})
                from app.services.db_client import _row_to_dict
                rows = [_row_to_dict(r) for r in cur.fetchall()]
        return jsonify({"errors": rows})
    except Exception as e:
        logger.error("api_errors: %s", e)
        return jsonify({"errors": []})


@admin_bp.route('/api/stats')
@_auth_required
def api_stats():
    try:
        from app.services.db_client import _row_to_dict
        with _get_conn() as conn:
            with conn.cursor() as cur:
                # total movements (non-error)
                cur.execute("SELECT COUNT(*) AS c FROM movements WHERE movement_type != 'ERROR'")
                total_movements = cur.fetchone()['c']

                # pallets in/out
                cur.execute("""
                    SELECT
                        SUM(CASE WHEN current_status='IN_WAREHOUSE'      THEN 1 ELSE 0 END) AS in_c,
                        SUM(CASE WHEN current_status='OUT_OF_WAREHOUSE'  THEN 1 ELSE 0 END) AS out_c
                    FROM pallet_status
                """)
                row = cur.fetchone()
                pallets_in  = row['in_c']  or 0
                pallets_out = row['out_c'] or 0

                # overrides
                cur.execute("SELECT COUNT(*) AS c FROM movements WHERE validation_status='OVERRIDE'")
                overrides = cur.fetchone()['c']

                # errors
                cur.execute("SELECT COUNT(*) AS c FROM movements WHERE movement_type='ERROR' OR validation_status NOT IN ('VALID','OVERRIDE')")
                errors = cur.fetchone()['c']

                # IVT scans today (Dubai time)
                cur.execute("""
                    SELECT COUNT(*) AS c FROM ivt_scans
                    WHERE DATE(scanned_date::timestamptz AT TIME ZONE 'Asia/Dubai') = CURRENT_DATE
                """)
                ivt_today = cur.fetchone()['c']

                # total IVT scans
                cur.execute("SELECT COUNT(*) AS c FROM ivt_scans")
                ivt_total = cur.fetchone()['c']

        return jsonify({
            "total_movements": int(total_movements),
            "total_errors":    int(errors),
            "pallets_in":      int(pallets_in),
            "pallets_out":     int(pallets_out),
            "override_count":  int(overrides),
            "pending_keys":    pending_count(),
            "ivt_today":       int(ivt_today),
            "ivt_total":       int(ivt_total),
        })
    except Exception as e:
        logger.error("api_stats: %s", e)
        return jsonify({"error": str(e)}), 500


@admin_bp.route('/api/connection-test')
@_auth_required
def api_conn_test():
    ok = db_ping()
    return jsonify({"status": "ok" if ok else "error", "db": ok})


# ── new analytics endpoints ───────────────────────────────────────────────────

@admin_bp.route('/api/analytics/velocity')
@_auth_required
def api_velocity():
    """Daily movement counts (last 30 days, Dubai time) split by type."""
    try:
        from app.services.db_client import _row_to_dict
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        TO_CHAR(timestamp_utc AT TIME ZONE 'Asia/Dubai', 'YYYY-MM-DD') AS day,
                        movement_type,
                        COUNT(*) AS cnt
                    FROM movements
                    WHERE movement_type IN ('STOCK_IN', 'STOCK_OUT')
                      AND timestamp_utc >= NOW() - INTERVAL '30 days'
                    GROUP BY day, movement_type
                    ORDER BY day
                """)
                rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"rows": rows})
    except Exception as e:
        logger.error("api_velocity: %s", e)
        return jsonify({"rows": []})


@admin_bp.route('/api/analytics/by-location')
@_auth_required
def api_by_location():
    """Pallets currently IN_WAREHOUSE grouped by location."""
    try:
        from app.services.db_client import _row_to_dict
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COALESCE(NULLIF(last_location,''), 'Unknown') AS location,
                        COUNT(*) AS pallet_count
                    FROM pallet_status
                    WHERE current_status = 'IN_WAREHOUSE'
                    GROUP BY last_location
                    ORDER BY pallet_count DESC
                """)
                rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"rows": rows})
    except Exception as e:
        logger.error("api_by_location: %s", e)
        return jsonify({"rows": []})


@admin_bp.route('/api/analytics/by-part')
@_auth_required
def api_by_part():
    """Top 15 parts currently in warehouse by pallet count."""
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COALESCE(NULLIF(indus_part,''), 'Unknown') AS part,
                        COUNT(*) AS pallet_count
                    FROM pallet_status
                    WHERE current_status = 'IN_WAREHOUSE'
                    GROUP BY indus_part
                    ORDER BY pallet_count DESC
                    LIMIT 15
                """)
                rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"rows": rows})
    except Exception as e:
        logger.error("api_by_part: %s", e)
        return jsonify({"rows": []})


@admin_bp.route('/api/analytics/operators')
@_auth_required
def api_operators():
    """Per-operator movement totals for the last 30 days."""
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COALESCE(NULLIF(operator_id,''), 'Unknown') AS operator,
                        movement_type,
                        COUNT(*) AS cnt,
                        MAX(timestamp_utc AT TIME ZONE 'Asia/Dubai') AS last_active
                    FROM movements
                    WHERE movement_type IN ('STOCK_IN','STOCK_OUT')
                      AND timestamp_utc >= NOW() - INTERVAL '30 days'
                    GROUP BY operator_id, movement_type
                    ORDER BY cnt DESC
                """)
                rows = []
                for r in cur.fetchall():
                    d = dict(r)
                    if hasattr(d.get('last_active'), 'isoformat'):
                        d['last_active'] = d['last_active'].isoformat()
                    rows.append(d)
        return jsonify({"rows": rows})
    except Exception as e:
        logger.error("api_operators: %s", e)
        return jsonify({"rows": []})


@admin_bp.route('/api/analytics/ivt')
@_auth_required
def api_ivt():
    """Recent IVT scans + daily counts for last 14 days."""
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT code, scanned_date, location
                    FROM ivt_scans
                    ORDER BY id DESC
                    LIMIT 100
                """)
                recent = [dict(r) for r in cur.fetchall()]

                cur.execute("""
                    SELECT
                        TO_CHAR(scanned_date::timestamptz AT TIME ZONE 'Asia/Dubai', 'YYYY-MM-DD') AS day,
                        COUNT(*) AS cnt
                    FROM ivt_scans
                    WHERE scanned_date::timestamptz >= NOW() - INTERVAL '14 days'
                    GROUP BY day
                    ORDER BY day
                """)
                daily = [dict(r) for r in cur.fetchall()]

        return jsonify({"recent": recent, "daily": daily})
    except Exception as e:
        logger.error("api_ivt: %s", e)
        return jsonify({"recent": [], "daily": []})
