"""
Admin dashboard — basic HTTP auth protected.

Features:
  - Filtered movements endpoint (HU, part, date, type, location)
  - Stock summary by material + location
  - Total stock summary across all locations
  - Negative stock detection in error log
  - Excel export (filter-aware, server-side via openpyxl)
  - Analytics: velocity, by-location, by-part, operators, IVT
"""
import io
import logging
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, render_template, request, Response, jsonify, current_app, send_file
from app.services.db_client import (
    get_all_pallet_statuses,
    ping as db_ping,
    _get_conn,
    _row_to_dict,
)
from app.utils.idempotency import pending_count

logger = logging.getLogger(__name__)
admin_bp = Blueprint('admin', __name__)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _auth_required(f):
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


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@admin_bp.route('/')
@_auth_required
def dashboard():
    return render_template('admin/dashboard.html')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_movement_filters(args):
    """
    Parse query-string filter params and return (WHERE clauses list, params dict).
    Supported: hu, part, date_from, date_to, movement_type, location, limit.
    """
    clauses, params = [], {}

    hu = (args.get('hu') or '').strip()
    if hu:
        clauses.append("handling_unit ILIKE %(hu)s")
        params['hu'] = f'%{hu}%'

    part = (args.get('part') or '').strip()
    if part:
        clauses.append("indus_part ILIKE %(part)s")
        params['part'] = f'%{part}%'

    date_from = (args.get('date_from') or '').strip()
    if date_from:
        clauses.append("timestamp_utc >= %(date_from)s::date")
        params['date_from'] = date_from

    date_to = (args.get('date_to') or '').strip()
    if date_to:
        clauses.append("timestamp_utc < (%(date_to)s::date + INTERVAL '1 day')")
        params['date_to'] = date_to

    mt = (args.get('movement_type') or '').strip().upper()
    if mt and mt != 'ALL':
        clauses.append("movement_type = %(movement_type)s")
        params['movement_type'] = mt

    loc = (args.get('location') or '').strip()
    if loc and loc != 'ALL':
        clauses.append("location = %(location)s")
        params['location'] = loc

    return clauses, params


def _safe_int(val, default=0):
    """Safely convert a value to int, returning default on failure."""
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Core data endpoints
# ---------------------------------------------------------------------------

@admin_bp.route('/api/movements')
@_auth_required
def api_movements():
    """Filtered movements. Params: hu, part, date_from, date_to, movement_type, location, limit."""
    limit = request.args.get('limit', 200, type=int)
    clauses, params = _build_movement_filters(request.args)
    params['limit'] = limit

    where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
    sql = f"""
        SELECT
            id, event_id,
            timestamp_utc AT TIME ZONE 'Asia/Dubai' AS timestamp_dubai,
            handling_unit, indus_part, customer_part, pallet_qty,
            job_no, movement_type, operator_id, device_id,
            sync_status, validation_status, location, from_location, to_location
        FROM movements
        {where}
        ORDER BY timestamp_utc DESC
        LIMIT %(limit)s
    """
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = [_row_to_dict(r) for r in cur.fetchall()]
        return jsonify({"movements": rows})
    except Exception as e:
        logger.error("api_movements: %s", e)
        return jsonify({"movements": [], "error": str(e)})


@admin_bp.route('/api/pallet-statuses')
@_auth_required
def api_pallet_statuses():
    hu   = (request.args.get('hu')   or '').strip()
    part = (request.args.get('part') or '').strip()
    loc  = (request.args.get('location') or '').strip()
    clauses, params = [], {}
    if hu:   clauses.append("handling_unit ILIKE %(hu)s");   params['hu']   = f'%{hu}%'
    if part: clauses.append("indus_part ILIKE %(part)s");   params['part'] = f'%{part}%'
    if loc and loc != 'ALL': clauses.append("last_location = %(loc)s"); params['loc'] = loc
    where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
    sql = f"""
        SELECT handling_unit, current_status, last_event_id,
               last_movement_type,
               last_movement_time AT TIME ZONE 'Asia/Dubai' AS last_movement_time_dubai,
               indus_part, pallet_qty, last_location
        FROM pallet_status
        {where}
        ORDER BY last_movement_time DESC NULLS LAST
    """
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = [_row_to_dict(r) for r in cur.fetchall()]
        return jsonify({"statuses": rows})
    except Exception as e:
        logger.error("api_pallet_statuses: %s", e)
        return jsonify({"statuses": []})


@admin_bp.route('/api/errors')
@_auth_required
def api_errors():
    """
    Error log combining:
      1. Movement rows with movement_type='ERROR' or invalid validation_status
      2. Materials/locations where net stock (IN - OUT) < 0 (negative stock alerts)
    """
    limit = request.args.get('limit', 200, type=int)
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                # --- movement errors ---
                cur.execute("""
                    SELECT
                        event_id,
                        timestamp_utc AT TIME ZONE 'Asia/Dubai' AS timestamp_dubai,
                        handling_unit, movement_type, validation_status,
                        operator_id, location, sync_status,
                        'MOVEMENT_ERROR' AS error_category,
                        NULL AS indus_part,
                        NULL AS net_qty
                    FROM movements
                    WHERE movement_type = 'ERROR'
                       OR validation_status NOT IN ('VALID', 'OVERRIDE')
                    ORDER BY timestamp_utc DESC
                    LIMIT %(limit)s
                """, {'limit': limit})
                movement_errors = [_row_to_dict(r) for r in cur.fetchall()]

                # --- negative stock: part+location combos where IN qty < OUT qty ---
                cur.execute("""
                    SELECT
                        COALESCE(NULLIF(indus_part,''), 'Unknown') AS indus_part,
                        COALESCE(NULLIF(location,''), 'Unknown')   AS location,
                        SUM(CASE WHEN movement_type='STOCK_IN'  THEN COALESCE(pallet_qty::int,0) ELSE 0 END) AS total_in,
                        SUM(CASE WHEN movement_type='STOCK_OUT' THEN COALESCE(pallet_qty::int,0) ELSE 0 END) AS total_out,
                        SUM(CASE WHEN movement_type='STOCK_IN'  THEN COALESCE(pallet_qty::int,0) ELSE 0 END)
                        - SUM(CASE WHEN movement_type='STOCK_OUT' THEN COALESCE(pallet_qty::int,0) ELSE 0 END) AS net_qty
                    FROM movements
                    WHERE movement_type IN ('STOCK_IN','STOCK_OUT')
                      AND pallet_qty ~ '^[0-9]+$'
                    GROUP BY indus_part, location
                    HAVING SUM(CASE WHEN movement_type='STOCK_IN'  THEN COALESCE(pallet_qty::int,0) ELSE 0 END)
                         - SUM(CASE WHEN movement_type='STOCK_OUT' THEN COALESCE(pallet_qty::int,0) ELSE 0 END) < 0
                    ORDER BY net_qty ASC
                """)
                neg_rows = [dict(r) for r in cur.fetchall()]

        return jsonify({"errors": movement_errors, "negative_stock": neg_rows})
    except Exception as e:
        logger.error("api_errors: %s", e)
        return jsonify({"errors": [], "negative_stock": []})


@admin_bp.route('/api/stats')
@_auth_required
def api_stats():
    """
    KPI stats. Each query is individually guarded so one failure
    does NOT zero out all stats — partial data is returned instead.
    """
    result = {
        "total_movements": None,
        "total_errors":    None,
        "pallets_in":      None,
        "pallets_out":     None,
        "override_count":  None,
        "pending_keys":    None,
        "ivt_today":       None,
        "ivt_total":       None,
        "neg_alerts":      None,
    }

    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:

                try:
                    cur.execute("SELECT COUNT(*) FROM movements WHERE movement_type != 'ERROR'")
                    result["total_movements"] = _safe_int(cur.fetchone()[0])
                except Exception as e:
                    logger.warning("stats total_movements: %s", e)

                try:
                    cur.execute("""
                        SELECT
                            SUM(CASE WHEN current_status='IN_WAREHOUSE'     THEN 1 ELSE 0 END),
                            SUM(CASE WHEN current_status='OUT_OF_WAREHOUSE' THEN 1 ELSE 0 END)
                        FROM pallet_status
                    """)
                    row = cur.fetchone()
                    if row:
                        result["pallets_in"]  = _safe_int(row[0])
                        result["pallets_out"] = _safe_int(row[1])
                except Exception as e:
                    logger.warning("stats pallets: %s", e)

                try:
                    cur.execute("SELECT COUNT(*) FROM movements WHERE validation_status='OVERRIDE'")
                    result["override_count"] = _safe_int(cur.fetchone()[0])
                except Exception as e:
                    logger.warning("stats overrides: %s", e)

                try:
                    cur.execute("""
                        SELECT COUNT(*) FROM movements
                        WHERE movement_type='ERROR'
                           OR validation_status NOT IN ('VALID','OVERRIDE')
                    """)
                    result["total_errors"] = _safe_int(cur.fetchone()[0])
                except Exception as e:
                    logger.warning("stats errors: %s", e)

                try:
                    cur.execute("""
                        SELECT COUNT(*) FROM ivt_scans
                        WHERE DATE(scanned_date::timestamptz AT TIME ZONE 'Asia/Dubai') = CURRENT_DATE
                    """)
                    result["ivt_today"] = _safe_int(cur.fetchone()[0])
                except Exception as e:
                    logger.warning("stats ivt_today: %s", e)

                try:
                    cur.execute("SELECT COUNT(*) FROM ivt_scans")
                    result["ivt_total"] = _safe_int(cur.fetchone()[0])
                except Exception as e:
                    logger.warning("stats ivt_total: %s", e)

                try:
                    cur.execute("""
                        SELECT COUNT(*) FROM (
                            SELECT indus_part, location
                            FROM movements
                            WHERE movement_type IN ('STOCK_IN','STOCK_OUT')
                              AND pallet_qty ~ '^[0-9]+$'
                            GROUP BY indus_part, location
                            HAVING
                                SUM(CASE WHEN movement_type='STOCK_IN'  THEN pallet_qty::int ELSE 0 END)
                              - SUM(CASE WHEN movement_type='STOCK_OUT' THEN pallet_qty::int ELSE 0 END) < 0
                        ) t
                    """)
                    result["neg_alerts"] = _safe_int(cur.fetchone()[0])
                except Exception as e:
                    logger.warning("stats neg_alerts: %s", e)

    except Exception as e:
        logger.error("api_stats DB connection failed: %s", e)
        # still return what we have — don't 500

    # pending_count() is in-memory, always safe
    try:
        result["pending_keys"] = pending_count()
    except Exception as e:
        logger.warning("stats pending_keys: %s", e)
        result["pending_keys"] = 0

    return jsonify(result)


@admin_bp.route('/api/connection-test')
@_auth_required
def api_conn_test():
    ok = db_ping()
    return jsonify({"status": "ok" if ok else "error", "db": ok})


# ---------------------------------------------------------------------------
# Stock summary (by material + location)
# ---------------------------------------------------------------------------

@admin_bp.route('/api/stock-summary')
@_auth_required
def api_stock_summary():
    """
    Aggregate stock per material + location.
    Shows total IN qty, total OUT qty, net qty (IN - OUT), pallet count IN.
    Params: part (filter), location (filter).
    """
    part = (request.args.get('part') or '').strip()
    loc  = (request.args.get('location') or '').strip()
    clauses = ["movement_type IN ('STOCK_IN','STOCK_OUT')", "pallet_qty ~ '^[0-9]+$'"]
    params  = {}
    if part: clauses.append("indus_part ILIKE %(part)s");   params['part'] = f'%{part}%'
    if loc and loc != 'ALL': clauses.append("location = %(loc)s"); params['loc'] = loc
    where = 'WHERE ' + ' AND '.join(clauses)
    sql = f"""
        SELECT
            COALESCE(NULLIF(indus_part,''),  'Unknown') AS indus_part,
            COALESCE(NULLIF(customer_part,''),'Unknown') AS customer_part,
            COALESCE(NULLIF(location,''),    'Unknown') AS location,
            SUM(CASE WHEN movement_type='STOCK_IN'  THEN pallet_qty::int ELSE 0 END) AS total_in_qty,
            SUM(CASE WHEN movement_type='STOCK_OUT' THEN pallet_qty::int ELSE 0 END) AS total_out_qty,
            SUM(CASE WHEN movement_type='STOCK_IN'  THEN pallet_qty::int ELSE 0 END)
            - SUM(CASE WHEN movement_type='STOCK_OUT' THEN pallet_qty::int ELSE 0 END) AS net_qty,
            COUNT(DISTINCT CASE WHEN movement_type='STOCK_IN' THEN handling_unit END) AS pallets_in,
            COUNT(DISTINCT CASE WHEN movement_type='STOCK_OUT' THEN handling_unit END) AS pallets_out
        FROM movements
        {where}
        GROUP BY indus_part, customer_part, location
        ORDER BY indus_part, location
    """
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"rows": rows})
    except Exception as e:
        logger.error("api_stock_summary: %s", e)
        return jsonify({"rows": [], "error": str(e)})


# ---------------------------------------------------------------------------
# Total stock summary (all materials, across all locations combined)
# ---------------------------------------------------------------------------

@admin_bp.route('/api/total-stock')
@_auth_required
def api_total_stock():
    """
    Aggregate stock per material across ALL locations combined.
    Useful for seeing total on-hand qty regardless of where it is stored.
    Params: part (filter).
    """
    part = (request.args.get('part') or '').strip()
    clauses = ["movement_type IN ('STOCK_IN','STOCK_OUT')", "pallet_qty ~ '^[0-9]+$'"]
    params  = {}
    if part:
        clauses.append("indus_part ILIKE %(part)s")
        params['part'] = f'%{part}%'
    where = 'WHERE ' + ' AND '.join(clauses)
    sql = f"""
        SELECT
            COALESCE(NULLIF(indus_part,''),   'Unknown') AS indus_part,
            COALESCE(NULLIF(customer_part,''),'Unknown') AS customer_part,
            SUM(CASE WHEN movement_type='STOCK_IN'  THEN pallet_qty::int ELSE 0 END) AS total_in_qty,
            SUM(CASE WHEN movement_type='STOCK_OUT' THEN pallet_qty::int ELSE 0 END) AS total_out_qty,
            SUM(CASE WHEN movement_type='STOCK_IN'  THEN pallet_qty::int ELSE 0 END)
            - SUM(CASE WHEN movement_type='STOCK_OUT' THEN pallet_qty::int ELSE 0 END) AS net_qty,
            COUNT(DISTINCT CASE WHEN movement_type='STOCK_IN'  THEN handling_unit END) AS pallets_in,
            COUNT(DISTINCT CASE WHEN movement_type='STOCK_OUT' THEN handling_unit END) AS pallets_out,
            COUNT(DISTINCT location) AS location_count
        FROM movements
        {where}
        GROUP BY indus_part, customer_part
        ORDER BY indus_part
    """
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"rows": rows})
    except Exception as e:
        logger.error("api_total_stock: %s", e)
        return jsonify({"rows": [], "error": str(e)})


# ---------------------------------------------------------------------------
# Excel export (filter-aware, openpyxl)
# ---------------------------------------------------------------------------

@admin_bp.route('/api/export')
@_auth_required
def api_export():
    """
    Export filtered data as .xlsx.
    Param: sheet = movements | statuses | stock | total_stock | errors | ivt (default: movements)
    All movement filter params (hu, part, date_from, date_to, movement_type, location) apply.
    """
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        logger.error("openpyxl not installed — cannot export")
        return Response(
            "openpyxl is not installed on this server. "
            "Add 'openpyxl>=3.1.2' to requirements.txt and redeploy.",
            status=500, mimetype='text/plain'
        )

    sheet_name = (request.args.get('sheet') or 'movements').lower()
    wb = openpyxl.Workbook()
    ws = wb.active

    HDR_FILL  = PatternFill("solid", fgColor="1E293B")
    HDR_FONT  = Font(bold=True, color="E2E8F0", size=10)
    ALT_FILL  = PatternFill("solid", fgColor="0F172A")
    NEG_FILL  = PatternFill("solid", fgColor="7F1D1D")

    def _style_header(ws, num_cols):
        for cell in ws[1]:
            cell.fill = HDR_FILL
            cell.font = HDR_FONT
            cell.alignment = Alignment(horizontal='center')
        for j in range(1, num_cols + 1):
            ws.column_dimensions[get_column_letter(j)].width = 22

    def _write_rows(ws, headers, data, neg_col=None):
        """Write data rows. neg_col = key name to check for negative value highlight."""
        for i, row in enumerate(data, start=2):
            ws.append([str(row.get(h, '') or '') for h in headers])
            if i % 2 == 0:
                for cell in ws[i]:
                    cell.fill = ALT_FILL
            if neg_col:
                try:
                    if int(row.get(neg_col, 0) or 0) < 0:
                        for cell in ws[i]:
                            cell.fill = NEG_FILL
                except (TypeError, ValueError):
                    pass

    try:
        if sheet_name == 'movements':
            clauses, params = _build_movement_filters(request.args)
            params['limit'] = request.args.get('limit', 5000, type=int)
            where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT event_id,
                               timestamp_utc AT TIME ZONE 'Asia/Dubai' AS timestamp_dubai,
                               handling_unit, indus_part, customer_part, pallet_qty,
                               job_no, movement_type, operator_id, device_id,
                               sync_status, validation_status, location, from_location, to_location
                        FROM movements {where}
                        ORDER BY timestamp_utc DESC LIMIT %(limit)s
                    """, params)
                    data = [_row_to_dict(r) for r in cur.fetchall()]
            ws.title = "Movements"
            headers = ['event_id','timestamp_dubai','handling_unit','indus_part','customer_part',
                       'pallet_qty','job_no','movement_type','operator_id','device_id',
                       'sync_status','validation_status','location','from_location','to_location']
            ws.append(headers)
            _style_header(ws, len(headers))
            _write_rows(ws, headers, data)
            filename = "movements_export.xlsx"

        elif sheet_name == 'statuses':
            hu   = (request.args.get('hu')   or '').strip()
            part = (request.args.get('part') or '').strip()
            loc  = (request.args.get('location') or '').strip()
            cl2, p2 = [], {}
            if hu:   cl2.append("handling_unit ILIKE %(hu)s");  p2['hu']   = f'%{hu}%'
            if part: cl2.append("indus_part ILIKE %(part)s");   p2['part'] = f'%{part}%'
            if loc and loc != 'ALL': cl2.append("last_location=%(loc)s"); p2['loc'] = loc
            where2 = ('WHERE ' + ' AND '.join(cl2)) if cl2 else ''
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT handling_unit, current_status, last_movement_type,
                               last_movement_time AT TIME ZONE 'Asia/Dubai' AS last_movement_time_dubai,
                               indus_part, pallet_qty, last_location
                        FROM pallet_status {where2}
                        ORDER BY last_movement_time DESC NULLS LAST
                    """, p2)
                    data = [_row_to_dict(r) for r in cur.fetchall()]
            ws.title = "Pallet Status"
            headers = ['handling_unit','current_status','last_movement_type',
                       'last_movement_time_dubai','indus_part','pallet_qty','last_location']
            ws.append(headers)
            _style_header(ws, len(headers))
            _write_rows(ws, headers, data)
            filename = "pallet_status_export.xlsx"

        elif sheet_name == 'stock':
            part = (request.args.get('part') or '').strip()
            loc  = (request.args.get('location') or '').strip()
            cl3 = ["movement_type IN ('STOCK_IN','STOCK_OUT')", "pallet_qty ~ '^[0-9]+$'"]
            p3  = {}
            if part: cl3.append("indus_part ILIKE %(part)s");   p3['part'] = f'%{part}%'
            if loc and loc != 'ALL': cl3.append("location=%(loc)s"); p3['loc'] = loc
            where3 = 'WHERE ' + ' AND '.join(cl3)
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT
                            COALESCE(NULLIF(indus_part,''),'Unknown')    AS indus_part,
                            COALESCE(NULLIF(customer_part,''),'Unknown') AS customer_part,
                            COALESCE(NULLIF(location,''),'Unknown')      AS location,
                            SUM(CASE WHEN movement_type='STOCK_IN'  THEN pallet_qty::int ELSE 0 END) AS total_in_qty,
                            SUM(CASE WHEN movement_type='STOCK_OUT' THEN pallet_qty::int ELSE 0 END) AS total_out_qty,
                            SUM(CASE WHEN movement_type='STOCK_IN'  THEN pallet_qty::int ELSE 0 END)
                            - SUM(CASE WHEN movement_type='STOCK_OUT' THEN pallet_qty::int ELSE 0 END) AS net_qty,
                            COUNT(DISTINCT CASE WHEN movement_type='STOCK_IN'  THEN handling_unit END) AS pallets_in,
                            COUNT(DISTINCT CASE WHEN movement_type='STOCK_OUT' THEN handling_unit END) AS pallets_out
                        FROM movements {where3}
                        GROUP BY indus_part, customer_part, location
                        ORDER BY indus_part, location
                    """, p3)
                    data = [dict(r) for r in cur.fetchall()]
            ws.title = "Stock Summary"
            headers = ['indus_part','customer_part','location','total_in_qty',
                       'total_out_qty','net_qty','pallets_in','pallets_out']
            ws.append(headers)
            _style_header(ws, len(headers))
            _write_rows(ws, headers, data, neg_col='net_qty')
            filename = "stock_summary_export.xlsx"

        elif sheet_name == 'total_stock':
            part = (request.args.get('part') or '').strip()
            cl4 = ["movement_type IN ('STOCK_IN','STOCK_OUT')", "pallet_qty ~ '^[0-9]+$'"]
            p4  = {}
            if part:
                cl4.append("indus_part ILIKE %(part)s")
                p4['part'] = f'%{part}%'
            where4 = 'WHERE ' + ' AND '.join(cl4)
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT
                            COALESCE(NULLIF(indus_part,''),'Unknown')    AS indus_part,
                            COALESCE(NULLIF(customer_part,''),'Unknown') AS customer_part,
                            SUM(CASE WHEN movement_type='STOCK_IN'  THEN pallet_qty::int ELSE 0 END) AS total_in_qty,
                            SUM(CASE WHEN movement_type='STOCK_OUT' THEN pallet_qty::int ELSE 0 END) AS total_out_qty,
                            SUM(CASE WHEN movement_type='STOCK_IN'  THEN pallet_qty::int ELSE 0 END)
                            - SUM(CASE WHEN movement_type='STOCK_OUT' THEN pallet_qty::int ELSE 0 END) AS net_qty,
                            COUNT(DISTINCT CASE WHEN movement_type='STOCK_IN'  THEN handling_unit END) AS pallets_in,
                            COUNT(DISTINCT CASE WHEN movement_type='STOCK_OUT' THEN handling_unit END) AS pallets_out,
                            COUNT(DISTINCT location) AS location_count
                        FROM movements {where4}
                        GROUP BY indus_part, customer_part
                        ORDER BY indus_part
                    """, p4)
                    data = [dict(r) for r in cur.fetchall()]
            ws.title = "Total Stock"
            headers = ['indus_part','customer_part','total_in_qty','total_out_qty',
                       'net_qty','pallets_in','pallets_out','location_count']
            ws.append(headers)
            _style_header(ws, len(headers))
            _write_rows(ws, headers, data, neg_col='net_qty')
            filename = "total_stock_export.xlsx"

        elif sheet_name == 'errors':
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT event_id,
                               timestamp_utc AT TIME ZONE 'Asia/Dubai' AS timestamp_dubai,
                               handling_unit, movement_type, validation_status,
                               operator_id, location, sync_status
                        FROM movements
                        WHERE movement_type='ERROR'
                           OR validation_status NOT IN ('VALID','OVERRIDE')
                        ORDER BY timestamp_utc DESC LIMIT 2000
                    """)
                    mov_errors = [_row_to_dict(r) for r in cur.fetchall()]
                    cur.execute("""
                        SELECT
                            COALESCE(NULLIF(indus_part,''),'Unknown') AS indus_part,
                            COALESCE(NULLIF(location,''),'Unknown')   AS location,
                            SUM(CASE WHEN movement_type='STOCK_IN'  THEN pallet_qty::int ELSE 0 END) AS total_in,
                            SUM(CASE WHEN movement_type='STOCK_OUT' THEN pallet_qty::int ELSE 0 END) AS total_out,
                            SUM(CASE WHEN movement_type='STOCK_IN'  THEN pallet_qty::int ELSE 0 END)
                            - SUM(CASE WHEN movement_type='STOCK_OUT' THEN pallet_qty::int ELSE 0 END) AS net_qty
                        FROM movements
                        WHERE movement_type IN ('STOCK_IN','STOCK_OUT')
                          AND pallet_qty ~ '^[0-9]+$'
                        GROUP BY indus_part, location
                        HAVING
                            SUM(CASE WHEN movement_type='STOCK_IN'  THEN pallet_qty::int ELSE 0 END)
                          - SUM(CASE WHEN movement_type='STOCK_OUT' THEN pallet_qty::int ELSE 0 END) < 0
                        ORDER BY net_qty
                    """)
                    neg = [dict(r) for r in cur.fetchall()]

            ws.title = "Movement Errors"
            h1 = ['event_id','timestamp_dubai','handling_unit','movement_type',
                  'validation_status','operator_id','location','sync_status']
            ws.append(h1)
            _style_header(ws, len(h1))
            _write_rows(ws, h1, mov_errors)

            ws2 = wb.create_sheet("Negative Stock")
            h2 = ['indus_part','location','total_in','total_out','net_qty']
            ws2.append(h2)
            for cell in ws2[1]:
                cell.fill = HDR_FILL
                cell.font = HDR_FONT
                cell.alignment = Alignment(horizontal='center')
            for j in range(1, len(h2) + 1):
                ws2.column_dimensions[get_column_letter(j)].width = 24
            for i, row in enumerate(neg, start=2):
                ws2.append([str(row.get(h, '') or '') for h in h2])
                for cell in ws2[i]:
                    cell.fill = NEG_FILL
            filename = "error_log_export.xlsx"

        elif sheet_name == 'ivt':
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT code, scanned_date, location FROM ivt_scans ORDER BY id DESC")
                    data = [dict(r) for r in cur.fetchall()]
            ws.title = "IVT Scans"
            headers = ['code', 'scanned_date', 'location']
            ws.append(headers)
            _style_header(ws, len(headers))
            _write_rows(ws, headers, data)
            filename = "ivt_scans_export.xlsx"

        else:
            return jsonify({"error": f"Unknown sheet: {sheet_name}"}), 400

    except Exception as e:
        logger.error("api_export sheet=%s: %s", sheet_name, e)
        return Response(f"Export failed: {e}", status=500, mimetype='text/plain')

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


# ---------------------------------------------------------------------------
# Analytics endpoints
# ---------------------------------------------------------------------------

@admin_bp.route('/api/analytics/velocity')
@_auth_required
def api_velocity():
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT TO_CHAR(timestamp_utc AT TIME ZONE 'Asia/Dubai','YYYY-MM-DD') AS day,
                           movement_type, COUNT(*) AS cnt
                    FROM movements
                    WHERE movement_type IN ('STOCK_IN','STOCK_OUT')
                      AND timestamp_utc >= NOW() - INTERVAL '30 days'
                    GROUP BY day, movement_type ORDER BY day
                """)
                rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"rows": rows})
    except Exception as e:
        logger.error("api_velocity: %s", e)
        return jsonify({"rows": []})


@admin_bp.route('/api/analytics/by-location')
@_auth_required
def api_by_location():
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COALESCE(NULLIF(last_location,''),'Unknown') AS location,
                           COUNT(*) AS pallet_count
                    FROM pallet_status WHERE current_status='IN_WAREHOUSE'
                    GROUP BY last_location ORDER BY pallet_count DESC
                """)
                rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"rows": rows})
    except Exception as e:
        logger.error("api_by_location: %s", e)
        return jsonify({"rows": []})


@admin_bp.route('/api/analytics/by-part')
@_auth_required
def api_by_part():
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COALESCE(NULLIF(indus_part,''),'Unknown') AS part,
                           COUNT(*) AS pallet_count
                    FROM pallet_status WHERE current_status='IN_WAREHOUSE'
                    GROUP BY indus_part ORDER BY pallet_count DESC LIMIT 15
                """)
                rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"rows": rows})
    except Exception as e:
        logger.error("api_by_part: %s", e)
        return jsonify({"rows": []})


@admin_bp.route('/api/analytics/operators')
@_auth_required
def api_operators():
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COALESCE(NULLIF(operator_id,''),'Unknown') AS operator,
                           movement_type, COUNT(*) AS cnt,
                           MAX(timestamp_utc AT TIME ZONE 'Asia/Dubai') AS last_active
                    FROM movements
                    WHERE movement_type IN ('STOCK_IN','STOCK_OUT')
                      AND timestamp_utc >= NOW() - INTERVAL '30 days'
                    GROUP BY operator_id, movement_type ORDER BY cnt DESC
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
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT code, scanned_date, location FROM ivt_scans ORDER BY id DESC LIMIT 100")
                recent = [dict(r) for r in cur.fetchall()]
                cur.execute("""
                    SELECT TO_CHAR(scanned_date::timestamptz AT TIME ZONE 'Asia/Dubai','YYYY-MM-DD') AS day,
                           COUNT(*) AS cnt
                    FROM ivt_scans
                    WHERE scanned_date::timestamptz >= NOW() - INTERVAL '14 days'
                    GROUP BY day ORDER BY day
                """)
                daily = [dict(r) for r in cur.fetchall()]
        return jsonify({"recent": recent, "daily": daily})
    except Exception as e:
        logger.error("api_ivt: %s", e)
        return jsonify({"recent": [], "daily": []})
