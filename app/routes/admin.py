"""
Admin dashboard — basic HTTP auth protected.

Features:
  - Filtered movements endpoint (HU, part, date, type, location)
  - Stock summary by material + location
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
                        indus_part,
                        location,
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
                neg_rows = []
                for r in cur.fetchall():
                    d = dict(r)
                    d['error_category']    = 'NEGATIVE_STOCK'
                    d['event_id']          = None
                    d['timestamp_dubai']   = None
                    d['handling_unit']     = None
                    d['movement_type']     = 'NEGATIVE_STOCK'
                    d['validation_status'] = 'STOCK_DEFICIT'
                    d['operator_id']       = None
                    d['sync_status']       = None
                    neg_rows.append(d)

        return jsonify({"errors": movement_errors, "negative_stock": neg_rows})
    except Exception as e:
        logger.error("api_errors: %s", e)
        return jsonify({"errors": [], "negative_stock": []})


@admin_bp.route('/api/stats')
@_auth_required
def api_stats():
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM movements WHERE movement_type != 'ERROR'")
                total_movements = cur.fetchone()['c']
                cur.execute("""
                    SELECT
                        SUM(CASE WHEN current_status='IN_WAREHOUSE'     THEN 1 ELSE 0 END) AS in_c,
                        SUM(CASE WHEN current_status='OUT_OF_WAREHOUSE' THEN 1 ELSE 0 END) AS out_c
                    FROM pallet_status
                """)
                row = cur.fetchone()
                cur.execute("SELECT COUNT(*) AS c FROM movements WHERE validation_status='OVERRIDE'")
                overrides = cur.fetchone()['c']
                cur.execute("SELECT COUNT(*) AS c FROM movements WHERE movement_type='ERROR' OR validation_status NOT IN ('VALID','OVERRIDE')")
                errors = cur.fetchone()['c']
                cur.execute("""
                    SELECT COUNT(*) AS c FROM ivt_scans
                    WHERE DATE(scanned_date::timestamptz AT TIME ZONE 'Asia/Dubai') = CURRENT_DATE
                """)
                ivt_today = cur.fetchone()['c']
                cur.execute("SELECT COUNT(*) AS c FROM ivt_scans")
                ivt_total = cur.fetchone()['c']
                # negative stock alerts count
                cur.execute("""
                    SELECT COUNT(*) AS c FROM (
                        SELECT indus_part, location
                        FROM movements
                        WHERE movement_type IN ('STOCK_IN','STOCK_OUT') AND pallet_qty ~ '^[0-9]+$'
                        GROUP BY indus_part, location
                        HAVING SUM(CASE WHEN movement_type='STOCK_IN'  THEN pallet_qty::int ELSE 0 END)
                             - SUM(CASE WHEN movement_type='STOCK_OUT' THEN pallet_qty::int ELSE 0 END) < 0
                    ) t
                """)
                neg_alerts = cur.fetchone()['c']
        return jsonify({
            "total_movements": int(total_movements),
            "total_errors":    int(errors),
            "pallets_in":      int(row['in_c']  or 0),
            "pallets_out":     int(row['out_c'] or 0),
            "override_count":  int(overrides),
            "pending_keys":    pending_count(),
            "ivt_today":       int(ivt_today),
            "ivt_total":       int(ivt_total),
            "neg_alerts":      int(neg_alerts),
        })
    except Exception as e:
        logger.error("api_stats: %s", e)
        return jsonify({"error": str(e)}), 500


@admin_bp.route('/api/connection-test')
@_auth_required
def api_conn_test():
    ok = db_ping()
    return jsonify({"status": "ok" if ok else "error", "db": ok})


# ---------------------------------------------------------------------------
# Stock summary
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
# Excel export (filter-aware)
# ---------------------------------------------------------------------------

@admin_bp.route('/api/export')
@_auth_required
def api_export():
    """
    Export filtered data as .xlsx.
    Param: sheet = movements | statuses | stock | errors | ivt (default: movements)
    All movement filter params (hu, part, date_from, date_to, movement_type, location) apply.
    """
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        return jsonify({"error": "openpyxl not installed"}), 500

    sheet_name = (request.args.get('sheet') or 'movements').lower()
    wb = openpyxl.Workbook()
    ws = wb.active

    HDR_FILL = PatternFill("solid", fgColor="1E293B")
    HDR_FONT = Font(bold=True, color="E2E8F0", size=10)
    ALT_FILL = PatternFill("solid", fgColor="0F172A")

    def write_sheet(headers, rows_of_dicts):
        ws.append(headers)
        for cell in ws[1]:
            cell.fill = HDR_FILL
            cell.font = HDR_FONT
            cell.alignment = Alignment(horizontal='center')
        for i, row in enumerate(rows_of_dicts, start=2):
            ws.append([row.get(h.lower().replace(' ','_').replace('(dubai)','dubai').replace('(','').replace(')',''), '') for h in headers])
            if i % 2 == 0:
                for cell in ws[i]:
                    cell.fill = ALT_FILL
        for col_idx, _ in enumerate(headers, 1):
            ws.column_dimensions[get_column_letter(col_idx)].width = 20

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
        for cell in ws[1]: cell.fill=HDR_FILL; cell.font=HDR_FONT; cell.alignment=Alignment(horizontal='center')
        for i, row in enumerate(data, start=2):
            ws.append([str(row.get(h,'') or '') for h in headers])
            if i%2==0:
                for cell in ws[i]: cell.fill=ALT_FILL
        for j in range(1, len(headers)+1):
            ws.column_dimensions[get_column_letter(j)].width = 22
        filename = "movements_export.xlsx"

    elif sheet_name == 'statuses':
        hu   = (request.args.get('hu')   or '').strip()
        part = (request.args.get('part') or '').strip()
        loc  = (request.args.get('location') or '').strip()
        clauses2, params2 = [], {}
        if hu:   clauses2.append("handling_unit ILIKE %(hu)s");  params2['hu']   = f'%{hu}%'
        if part: clauses2.append("indus_part ILIKE %(part)s");   params2['part'] = f'%{part}%'
        if loc and loc!='ALL': clauses2.append("last_location=%(loc)s"); params2['loc'] = loc
        where2 = ('WHERE ' + ' AND '.join(clauses2)) if clauses2 else ''
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT handling_unit, current_status, last_movement_type,
                           last_movement_time AT TIME ZONE 'Asia/Dubai' AS last_movement_time_dubai,
                           indus_part, pallet_qty, last_location
                    FROM pallet_status {where2}
                    ORDER BY last_movement_time DESC NULLS LAST
                """, params2)
                data = [_row_to_dict(r) for r in cur.fetchall()]
        ws.title = "Pallet Status"
        headers = ['handling_unit','current_status','last_movement_type','last_movement_time_dubai',
                   'indus_part','pallet_qty','last_location']
        ws.append(headers)
        for cell in ws[1]: cell.fill=HDR_FILL; cell.font=HDR_FONT; cell.alignment=Alignment(horizontal='center')
        for i, row in enumerate(data, start=2):
            ws.append([str(row.get(h,'') or '') for h in headers])
            if i%2==0:
                for cell in ws[i]: cell.fill=ALT_FILL
        for j in range(1, len(headers)+1):
            ws.column_dimensions[get_column_letter(j)].width = 24
        filename = "pallet_status_export.xlsx"

    elif sheet_name == 'stock':
        part = (request.args.get('part') or '').strip()
        loc  = (request.args.get('location') or '').strip()
        clauses3 = ["movement_type IN ('STOCK_IN','STOCK_OUT')","pallet_qty ~ '^[0-9]+$'"]
        params3  = {}
        if part: clauses3.append("indus_part ILIKE %(part)s");   params3['part'] = f'%{part}%'
        if loc and loc!='ALL': clauses3.append("location=%(loc)s"); params3['loc'] = loc
        where3 = 'WHERE ' + ' AND '.join(clauses3)
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COALESCE(NULLIF(indus_part,''),'Unknown')   AS indus_part,
                        COALESCE(NULLIF(customer_part,''),'Unknown') AS customer_part,
                        COALESCE(NULLIF(location,''),'Unknown')     AS location,
                        SUM(CASE WHEN movement_type='STOCK_IN'  THEN pallet_qty::int ELSE 0 END) AS total_in_qty,
                        SUM(CASE WHEN movement_type='STOCK_OUT' THEN pallet_qty::int ELSE 0 END) AS total_out_qty,
                        SUM(CASE WHEN movement_type='STOCK_IN'  THEN pallet_qty::int ELSE 0 END)
                        - SUM(CASE WHEN movement_type='STOCK_OUT' THEN pallet_qty::int ELSE 0 END) AS net_qty,
                        COUNT(DISTINCT CASE WHEN movement_type='STOCK_IN'  THEN handling_unit END) AS pallets_in,
                        COUNT(DISTINCT CASE WHEN movement_type='STOCK_OUT' THEN handling_unit END) AS pallets_out
                    FROM movements {where3}
                    GROUP BY indus_part, customer_part, location
                    ORDER BY indus_part, location
                """, params3)
                data = [dict(r) for r in cur.fetchall()]
        ws.title = "Stock Summary"
        headers = ['indus_part','customer_part','location','total_in_qty','total_out_qty','net_qty','pallets_in','pallets_out']
        ws.append(headers)
        for cell in ws[1]: cell.fill=HDR_FILL; cell.font=HDR_FONT; cell.alignment=Alignment(horizontal='center')
        for i, row in enumerate(data, start=2):
            ws.append([str(row.get(h,'') or '') for h in headers])
            if i%2==0:
                for cell in ws[i]: cell.fill=ALT_FILL
            # highlight negative net_qty rows in red
            try:
                net = int(row.get('net_qty',0) or 0)
                if net < 0:
                    for cell in ws[i]:
                        cell.fill = PatternFill("solid", fgColor="7F1D1D")
            except Exception:
                pass
        for j in range(1, len(headers)+1):
            ws.column_dimensions[get_column_letter(j)].width = 20
        filename = "stock_summary_export.xlsx"

    elif sheet_name == 'errors':
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT event_id,
                           timestamp_utc AT TIME ZONE 'Asia/Dubai' AS timestamp_dubai,
                           handling_unit, movement_type, validation_status,
                           operator_id, location, sync_status
                    FROM movements
                    WHERE movement_type='ERROR' OR validation_status NOT IN ('VALID','OVERRIDE')
                    ORDER BY timestamp_utc DESC LIMIT 2000
                """)
                mov_errors = [_row_to_dict(r) for r in cur.fetchall()]
                cur.execute("""
                    SELECT COALESCE(NULLIF(indus_part,''),'Unknown') AS indus_part,
                           COALESCE(NULLIF(location,''),'Unknown') AS location,
                           SUM(CASE WHEN movement_type='STOCK_IN'  THEN pallet_qty::int ELSE 0 END)
                           - SUM(CASE WHEN movement_type='STOCK_OUT' THEN pallet_qty::int ELSE 0 END) AS net_qty
                    FROM movements
                    WHERE movement_type IN ('STOCK_IN','STOCK_OUT') AND pallet_qty ~ '^[0-9]+$'
                    GROUP BY indus_part, location
                    HAVING SUM(CASE WHEN movement_type='STOCK_IN'  THEN pallet_qty::int ELSE 0 END)
                         - SUM(CASE WHEN movement_type='STOCK_OUT' THEN pallet_qty::int ELSE 0 END) < 0
                    ORDER BY net_qty
                """)
                neg = [dict(r) for r in cur.fetchall()]
        ws.title = "Movement Errors"
        h1 = ['event_id','timestamp_dubai','handling_unit','movement_type','validation_status','operator_id','location','sync_status']
        ws.append(h1)
        for cell in ws[1]: cell.fill=HDR_FILL; cell.font=HDR_FONT; cell.alignment=Alignment(horizontal='center')
        for i, row in enumerate(mov_errors, start=2):
            ws.append([str(row.get(h,'') or '') for h in h1])
            if i%2==0:
                for cell in ws[i]: cell.fill=ALT_FILL
        for j in range(1, len(h1)+1):
            ws.column_dimensions[get_column_letter(j)].width = 22
        # second sheet: negative stock
        ws2 = wb.create_sheet("Negative Stock")
        h2 = ['indus_part','location','net_qty']
        ws2.append(h2)
        for cell in ws2[1]: cell.fill=HDR_FILL; cell.font=HDR_FONT; cell.alignment=Alignment(horizontal='center')
        for i, row in enumerate(neg, start=2):
            ws2.append([str(row.get(h,'') or '') for h in h2])
            for cell in ws2[i]:
                cell.fill = PatternFill("solid", fgColor="7F1D1D")
        for j in range(1, len(h2)+1):
            ws2.column_dimensions[get_column_letter(j)].width = 24
        filename = "error_log_export.xlsx"

    elif sheet_name == 'ivt':
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT code, scanned_date, location FROM ivt_scans ORDER BY id DESC")
                data = [dict(r) for r in cur.fetchall()]
        ws.title = "IVT Scans"
        headers = ['code','scanned_date','location']
        ws.append(headers)
        for cell in ws[1]: cell.fill=HDR_FILL; cell.font=HDR_FONT; cell.alignment=Alignment(horizontal='center')
        for i, row in enumerate(data, start=2):
            ws.append([str(row.get(h,'') or '') for h in headers])
            if i%2==0:
                for cell in ws[i]: cell.fill=ALT_FILL
        for j in range(1, len(headers)+1):
            ws.column_dimensions[get_column_letter(j)].width = 36
        filename = "ivt_scans_export.xlsx"

    else:
        return jsonify({"error": f"Unknown sheet: {sheet_name}"}), 400

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
                    if hasattr(d.get('last_active'),'isoformat'): d['last_active'] = d['last_active'].isoformat()
                    rows.append(d)
        return jsonify({"rows": rows})
    except Exception as e:
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
        return jsonify({"recent": [], "daily": []})
