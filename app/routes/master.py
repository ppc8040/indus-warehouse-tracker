"""
Master Admin Portal — /master

Separate elevated-privilege portal above the regular admin tier.
Protected by HTTP Basic Auth with MASTER_USERNAME / MASTER_PASSWORD
(configured in config.py, overridable via env vars).

This blueprint is purely additive — zero modifications to admin.py
or any other existing route.
"""
import os
import json
import logging
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Blueprint, Response, current_app, g, jsonify,
    render_template, request
)

from app.db_client import get_db_connection

master_bp = Blueprint('master', __name__, template_folder='templates')
logger = logging.getLogger(__name__)


# ── Auth ─────────────────────────────────────────────────────────────────────

def _require_master_auth(f):
    """HTTP Basic Auth guard — credentials from config / env."""
    @wraps(f)
    def decorated(*args, **kwargs):
        cfg = current_app.config
        expected_user = cfg.get('MASTER_USERNAME', 'master_admin')
        expected_pass = cfg.get('MASTER_PASSWORD', 'Indus@8040')

        auth = request.authorization
        if not auth or auth.username != expected_user or auth.password != expected_pass:
            return Response(
                'Master Admin access required.',
                401,
                {'WWW-Authenticate': 'Basic realm="Master Admin"'}
            )
        return f(*args, **kwargs)
    return decorated


# ── Dashboard ─────────────────────────────────────────────────────────────────

@master_bp.route('/')
@_require_master_auth
def dashboard():
    return render_template('master/dashboard.html')


# ── API: System Config (sanitised) ───────────────────────────────────────────

@master_bp.route('/api/config')
@_require_master_auth
def api_config():
    """Return a sanitised view of the active app config."""
    cfg = current_app.config
    REDACTED = '••••••••'
    sensitive = {'SECRET_KEY', 'ADMIN_PASSWORD', 'MASTER_PASSWORD',
                 'GRAPH_CLIENT_SECRET', 'DATABASE_URL'}
    result = {}
    for k, v in cfg.items():
        if k.startswith('_'):
            continue
        if k in sensitive:
            result[k] = REDACTED
        elif isinstance(v, (str, int, float, bool, type(None))):
            result[k] = v
    return jsonify(result)


# ── API: Override Audit Log ───────────────────────────────────────────────────

@master_bp.route('/api/overrides')
@_require_master_auth
def api_overrides():
    """All OVERRIDE movements — filterable by operator and date."""
    operator = request.args.get('operator', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to   = request.args.get('date_to', '').strip()
    limit     = min(int(request.args.get('limit', 500)), 2000)

    conditions = ["validation_status = 'OVERRIDE'"]
    params     = []

    if operator:
        conditions.append('operator_id = %s')
        params.append(operator)
    if date_from:
        conditions.append('timestamp_dubai::date >= %s')
        params.append(date_from)
    if date_to:
        conditions.append('timestamp_dubai::date <= %s')
        params.append(date_to)

    where = ' AND '.join(conditions)
    sql = f"""
        SELECT handling_unit, indus_part, pallet_qty, movement_type,
               location, operator_id, timestamp_dubai, sync_status, validation_status
        FROM movements
        WHERE {where}
        ORDER BY timestamp_dubai DESC
        LIMIT %s
    """
    params.append(limit)

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                # Serialise datetimes
                for row in rows:
                    for k, v in row.items():
                        if isinstance(v, datetime):
                            row[k] = v.isoformat()
        return jsonify({'overrides': rows, 'count': len(rows)})
    except Exception as e:
        logger.error('api_overrides error: %s', e)
        return jsonify({'error': str(e)}), 500


# ── API: Operator Activity ────────────────────────────────────────────────────

@master_bp.route('/api/operators')
@_require_master_auth
def api_operators():
    """Per-operator stats: movement counts, last active, error rate."""
    sql = """
        SELECT
            operator_id,
            COUNT(*)                                              AS total_moves,
            SUM(CASE WHEN movement_type = 'STOCK_IN'  THEN 1 ELSE 0 END) AS moves_in,
            SUM(CASE WHEN movement_type = 'STOCK_OUT' THEN 1 ELSE 0 END) AS moves_out,
            SUM(CASE WHEN validation_status = 'OVERRIDE' THEN 1 ELSE 0 END) AS overrides,
            SUM(CASE WHEN validation_status NOT IN ('VALID','OVERRIDE') THEN 1 ELSE 0 END) AS errors,
            MAX(timestamp_dubai)                                  AS last_active
        FROM movements
        WHERE operator_id IS NOT NULL AND operator_id <> ''
        GROUP BY operator_id
        ORDER BY total_moves DESC
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                cols = [d[0] for d in cur.description]
                rows = []
                for r in cur.fetchall():
                    row = dict(zip(cols, r))
                    for k, v in row.items():
                        if isinstance(v, datetime):
                            row[k] = v.isoformat()
                    rows.append(row)
        return jsonify({'operators': rows})
    except Exception as e:
        logger.error('api_operators error: %s', e)
        return jsonify({'error': str(e)}), 500


# ── API: Manual Pallet Status Write ──────────────────────────────────────────

@master_bp.route('/api/pallet-patch', methods=['POST'])
@_require_master_auth
def api_pallet_patch():
    """
    Directly correct a pallet_status row.
    Body JSON: { hu, status, location, indus_part, pallet_qty, reason }
    """
    data = request.get_json(force=True)
    hu       = (data.get('hu') or '').strip().upper()
    status   = (data.get('status') or '').strip().upper()
    location = (data.get('location') or '').strip().upper()
    part     = (data.get('indus_part') or '').strip()
    qty      = data.get('pallet_qty')
    reason   = (data.get('reason') or 'Master admin manual correction').strip()

    if not hu:
        return jsonify({'error': 'hu is required'}), 400
    if status not in ('IN_WAREHOUSE', 'OUT_OF_WAREHOUSE', ''):
        return jsonify({'error': 'Invalid status value'}), 400

    update_fields = []
    params        = []

    if status:
        update_fields.append('current_status = %s')
        params.append(status)
    if location:
        update_fields.append('last_location = %s')
        params.append(location)
    if part:
        update_fields.append('indus_part = %s')
        params.append(part)
    if qty is not None:
        update_fields.append('pallet_qty = %s')
        params.append(int(qty))

    if not update_fields:
        return jsonify({'error': 'No fields to update'}), 400

    params.append(hu)
    sql = f"""
        UPDATE pallet_status
        SET {', '.join(update_fields)}
        WHERE handling_unit = %s
    """

    # Also log to master_audit table if it exists, else just log to stderr
    audit_sql = """
        INSERT INTO master_audit (action, handling_unit, payload, performed_at)
        VALUES ('PALLET_PATCH', %s, %s, NOW())
        ON CONFLICT DO NOTHING
    """

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                affected = cur.rowcount
                try:
                    cur.execute(audit_sql, (hu, json.dumps(data)))
                except Exception:
                    pass  # audit table may not exist yet; non-fatal
            conn.commit()
        logger.info('MASTER PALLET_PATCH: HU=%s fields=%s reason=%s affected=%d',
                    hu, update_fields, reason, affected)
        return jsonify({'ok': True, 'affected': affected})
    except Exception as e:
        logger.error('api_pallet_patch error: %s', e)
        return jsonify({'error': str(e)}), 500


# ── API: Delete Movement Row ──────────────────────────────────────────────────

@master_bp.route('/api/movement-delete', methods=['POST'])
@_require_master_auth
def api_movement_delete():
    """
    Hard-delete a movement row by its ID.
    Body JSON: { id, reason }
    """
    data   = request.get_json(force=True)
    row_id = data.get('id')
    reason = (data.get('reason') or 'Master admin delete').strip()

    if not row_id:
        return jsonify({'error': 'id is required'}), 400

    # Fetch the row first for audit
    fetch_sql  = 'SELECT * FROM movements WHERE id = %s'
    delete_sql = 'DELETE FROM movements WHERE id = %s'
    audit_sql  = """
        INSERT INTO master_audit (action, handling_unit, payload, performed_at)
        VALUES ('MOVEMENT_DELETE', %s, %s, NOW())
        ON CONFLICT DO NOTHING
    """

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(fetch_sql, (row_id,))
                row = cur.fetchone()
                if not row:
                    return jsonify({'error': f'No movement found with id={row_id}'}), 404
                cols     = [d[0] for d in cur.description]
                row_dict = dict(zip(cols, row))
                for k, v in row_dict.items():
                    if isinstance(v, datetime):
                        row_dict[k] = v.isoformat()

                cur.execute(delete_sql, (row_id,))
                try:
                    cur.execute(audit_sql, (
                        row_dict.get('handling_unit', ''),
                        json.dumps({'deleted_row': row_dict, 'reason': reason})
                    ))
                except Exception:
                    pass
            conn.commit()
        logger.warning('MASTER MOVEMENT_DELETE: id=%s hu=%s reason=%s',
                       row_id, row_dict.get('handling_unit'), reason)
        return jsonify({'ok': True, 'deleted_row': row_dict})
    except Exception as e:
        logger.error('api_movement_delete error: %s', e)
        return jsonify({'error': str(e)}), 500


# ── API: DB Health ────────────────────────────────────────────────────────────

@master_bp.route('/api/db-health')
@_require_master_auth
def api_db_health():
    """Extended DB health check with table row counts."""
    tables = ['movements', 'pallet_status', 'ivt_scans']
    result = {}
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                for t in tables:
                    try:
                        cur.execute(f'SELECT COUNT(*) FROM {t}')
                        result[t] = cur.fetchone()[0]
                    except Exception:
                        result[t] = None
                # Pending sync count
                try:
                    cur.execute(
                        "SELECT COUNT(*) FROM movements WHERE sync_status NOT IN ('SYNCED','FAILED_PERMANENT')"
                    )
                    result['pending_sync'] = cur.fetchone()[0]
                except Exception:
                    result['pending_sync'] = None
        result['status'] = 'ok'
        result['checked_at'] = datetime.now(timezone.utc).isoformat()
        return jsonify(result)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500


# ── API: Pending Sync Queue ───────────────────────────────────────────────────

@master_bp.route('/api/pending-sync')
@_require_master_auth
def api_pending_sync():
    """Movements still awaiting SAP sync — oldest first."""
    sql = """
        SELECT id, handling_unit, indus_part, movement_type,
               location, operator_id, timestamp_dubai, sync_status, sync_attempts
        FROM movements
        WHERE sync_status NOT IN ('SYNCED', 'FAILED_PERMANENT')
        ORDER BY timestamp_dubai ASC
        LIMIT 200
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                cols = [d[0] for d in cur.description]
                rows = []
                for r in cur.fetchall():
                    row = dict(zip(cols, r))
                    for k, v in row.items():
                        if isinstance(v, datetime):
                            row[k] = v.isoformat()
                    rows.append(row)
        return jsonify({'pending': rows, 'count': len(rows)})
    except Exception as e:
        logger.error('api_pending_sync error: %s', e)
        return jsonify({'error': str(e)}), 500


# ── API: Force Sync Single Movement ──────────────────────────────────────────

@master_bp.route('/api/force-sync', methods=['POST'])
@_require_master_auth
def api_force_sync():
    """
    Reset a movement's sync_status back to PENDING so the worker picks it up.
    Body JSON: { id }
    """
    data   = request.get_json(force=True)
    row_id = data.get('id')
    if not row_id:
        return jsonify({'error': 'id is required'}), 400

    sql = """
        UPDATE movements
        SET sync_status = 'PENDING', sync_attempts = 0
        WHERE id = %s
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (row_id,))
                affected = cur.rowcount
            conn.commit()
        logger.info('MASTER FORCE_SYNC: id=%s', row_id)
        return jsonify({'ok': True, 'affected': affected})
    except Exception as e:
        logger.error('api_force_sync error: %s', e)
        return jsonify({'error': str(e)}), 500
