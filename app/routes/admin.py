"""
Admin dashboard — basic HTTP auth protected.

Features:
  - Filtered movements endpoint (HU, part, date, type, location)
  - Stock summary pivoted by material — one row per part, one column per location
  - Total stock summary across all locations
  - Negative stock detection in error log + Stock tab banner
  - Excel export (filter-aware, server-side via openpyxl)
  - Analytics: velocity, by-location, by-part, operators, IVT
  - SAP Recon: compares scanner net qty vs SAP plant stock (Plants 1000 & 2000)
  - SAP Recon Status: lightweight polling endpoint for frontend progress tracking
  - OOR Customer Codes: reads Open Order Report (col H = Indus Part, col R = Customer Code)
  - BDR scans: list, stats (bdr_today / bdr_total), and Excel export
"""
import io
import time
import logging
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, render_template, request, Response, jsonify, current_app, send_file, redirect, url_for, session
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
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin.login'))
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

@admin_bp.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin.dashboard'))
    error = False
    if request.method == 'POST':
        pw = current_app.config.get('ADMIN_PASSWORD', 'indus2026')
        if request.form.get('password') == pw:
            session.permanent = True
            session['admin_logged_in'] = True
            return redirect(url_for('admin.dashboard'))
        error = True
    return render_template('admin/login.html', error=error)


@admin_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('admin.login'))

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


def _fetch_scalar(cur):
    """
    Fetch one row from a RealDictCursor COUNT(*) query and return the integer.
    RealDictRow does NOT support [0] index access — use the first value instead.
    """
    row = cur.fetchone()
    if row is None:
        return 0
    return _safe_int(next(iter(row.values())))


# NOTE on pallet_qty aggregation:
# pallet_qty may be stored as TEXT or INTEGER depending on the DB column type.
# We cast to TEXT first so REGEXP_REPLACE works regardless of the underlying type,
# then strip non-digits, fall back to '0' if nothing remains, and cast to int.
# This handles blank strings, NULL, and non-numeric values safely.
STOCK_QTY_EXPR = "COALESCE(pallet_qty, 0)"

# Known warehouse locations used for the pivot in api_stock_summary.
# Order here determines the column order in the UI and Excel export.
STOCK_LOCATIONS = ['M4', 'M5', 'M6', 'M4_STORE', 'M16_STORE']

# Columns written as integers in the Excel export.
_INT_EXPORT_COLS = frozenset({
    'pallet_qty',
    'total_in_qty', 'total_out_qty', 'net_qty',
    'pallets_in', 'pallets_out', 'location_count',
    'total_in', 'total_out',
    # pivot stock columns
    'm4_stock', 'm5_stock', 'm6_stock', 'm4_store_stock', 'm16_store_stock', 'total_stock',
    # SAP recon columns
    'scanner_qty', 'sap_qty_1000', 'sap_qty_2000', 'variance_vs_1000',
})

# Columns that carry formatted datetime strings ("27 Apr 2026, 09:33").
_DT_EXPORT_COLS = frozenset({
    'timestamp_dubai',
    'last_movement_time_dubai',
})
_DT_EXPORT_FMT  = 'DD MMM YYYY HH:MM'
_DT_PARSE_FMT   = '%d %b %Y, %H:%M'


# ---------------------------------------------------------------------------
# OOR Customer Codes — reads Indus Open Order Report New.xlsx from SharePoint
# Col H (index 7)  = Indus Part
# Col R (index 17) = Actual Customer Code
# Results are cached in-memory for 5 minutes to avoid hammering Graph API.
# ---------------------------------------------------------------------------

_oor_cache = {"data": None, "ts": 0.0}
_OOR_CACHE_TTL = 300  # seconds

_stats_cache = {"data": None, "ts": 0.0}
_STATS_CACHE_TTL = 60  # seconds — stats refresh every 60s max

_container_cache = {}
_CONTAINER_CACHE_TTL = 300  # 5 minutes


def _fetch_oor_customer_codes():
    """
    ... (docstring unchanged)
    """
    import requests as req_lib
    from urllib.parse import quote  # Add this import at the function level

    cfg = current_app.config
    tenant   = cfg['GRAPH_TENANT_ID']
    client   = cfg['GRAPH_CLIENT_ID']
    secret   = cfg['GRAPH_CLIENT_SECRET']
    site_id  = cfg['SHAREPOINT_SITE_ID']
    filename = cfg['WORKBOOK_FILENAME_OOR']
    timeout  = cfg.get('GRAPH_REQUEST_TIMEOUT', 30)

    # 1. Acquire OAuth2 token
    token_url = f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token'
    token_resp = req_lib.post(token_url, data={
        'grant_type':    'client_credentials',
        'client_id':     client,
        'client_secret': secret,
        'scope':         'https://graph.microsoft.com/.default',
    }, timeout=timeout)
    token_resp.raise_for_status()
    access_token = token_resp.json()['access_token']
    headers = {'Authorization': f'Bearer {access_token}'}

    # 2. Find the workbook DriveItem by filename
    search_url = (
        f'https://graph.microsoft.com/v1.0/sites/{site_id}'
        f'/drive/root/search(q=\'{filename}\')'
    )
    search_resp = req_lib.get(search_url, headers=headers, timeout=timeout)
    search_resp.raise_for_status()
    items = search_resp.json().get('value', [])
    item = next(
        (i for i in items if i.get('name', '').lower() == filename.lower()),
        None
    )
    if not item:
        logger.warning('OOR workbook not found: %s', filename)
        return {}

    item_id = item['id']

    # 3. Get all worksheets and pick the first one
    ws_url = (
        f'https://graph.microsoft.com/v1.0/sites/{site_id}'
        f'/drive/items/{item_id}/workbook/worksheets'
    )
    ws_resp = req_lib.get(ws_url, headers=headers, timeout=timeout)
    ws_resp.raise_for_status()
    sheets = ws_resp.json().get('value', [])
    if not sheets:
        logger.warning('OOR workbook has no sheets')
        return {}

    sheet_name = sheets[0]['name']
    for s in sheets:
        if 'open order' in s.get('name', '').lower():
            sheet_name = s['name']
            break

    # 4. Read used range — properly encode the sheet name
    encoded_sheet_name = quote(sheet_name)
    range_url = (
        f'https://graph.microsoft.com/v1.0/sites/{site_id}'
        f'/drive/items/{item_id}/workbook/worksheets(\'{encoded_sheet_name}\')'
        f'/usedRange'
    )
    range_resp = req_lib.get(range_url, headers=headers, timeout=60)
    range_resp.raise_for_status()
    all_rows = range_resp.json().get('values', [])

    if len(all_rows) < 2:
        return {}

    # 5. Build the lookup dict (rest unchanged)
    result = {}
    for row in all_rows[1:]:
        if len(row) <= 17:
            continue
        indus_part = str(row[7]).strip().upper()
        cust_code  = str(row[17]).strip().upper()
        if not indus_part or indus_part in ('', 'NONE', 'N/A', '#N/A'):
            continue
        if not cust_code or cust_code in ('', 'NONE', 'N/A', '#N/A'):
            continue
        if indus_part not in result:
            result[indus_part] = set()
        result[indus_part].add(cust_code)

    return {k: ', '.join(sorted(v)) for k, v in result.items()}

@admin_bp.route('/api/oor-customer-codes')
@_auth_required
def api_oor_customer_codes():
    """
    Returns a mapping of Indus Part -> Customer Code(s) from the Open Order
    Report SharePoint workbook.  Results are served from an in-memory cache
    for up to 5 minutes so rapid dashboard refreshes don't hammer the
    Graph API.

    Response:
        { "customer_codes": { "CEJ1773": "CPT", "AJM0488": "IVT, CPT" },
          "count": 142, "cached": false }
    """
    now = time.time()
    if _oor_cache['data'] is not None and (now - _oor_cache['ts']) < _OOR_CACHE_TTL:
        return jsonify({
            'customer_codes': _oor_cache['data'],
            'count':          len(_oor_cache['data']),
            'cached':         True,
        })

    try:
        codes = _fetch_oor_customer_codes()
        _oor_cache['data'] = codes
        _oor_cache['ts']   = now
        return jsonify({
            'customer_codes': codes,
            'count':          len(codes),
            'cached':         False,
        })
    except Exception as exc:
        logger.error('api_oor_customer_codes failed: %s', exc)
        # Return whatever is in cache (even if stale) rather than an empty dict
        fallback = _oor_cache['data'] or {}
        return jsonify({
            'customer_codes': fallback,
            'count':          len(fallback),
            'cached':         True,
            'error':          str(exc),
        })


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
        ORDER BY timestamp_utc DESC, id DESC
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
    """
    Returns the true latest status for every HU by deriving it live from the
    movements table (DISTINCT ON handling_unit ORDER BY timestamp_utc DESC, id DESC).
    """
    hu   = (request.args.get('hu')       or '').strip()
    part = (request.args.get('part')     or '').strip()
    loc  = (request.args.get('location') or '').strip()

    outer_clauses = []
    params = {}
    if hu:
        outer_clauses.append("m.handling_unit ILIKE %(hu)s")
        params['hu'] = f'%{hu}%'
    if part:
        outer_clauses.append(
            "(COALESCE(m.indus_part, ps.indus_part)) ILIKE %(part)s"
        )
        params['part'] = f'%{part}%'
    if loc and loc != 'ALL':
        outer_clauses.append(
            "COALESCE(NULLIF(m.location,''), ps.last_location) = %(loc)s"
        )
        params['loc'] = loc

    outer_where = ('WHERE ' + ' AND '.join(outer_clauses)) if outer_clauses else ''

    sql = f"""
        WITH latest_movement AS (
            SELECT DISTINCT ON (handling_unit)
                handling_unit,
                movement_type        AS last_movement_type,
                timestamp_utc        AS last_movement_time,
                location,
                indus_part,
                pallet_qty,
                event_id             AS last_event_id
            FROM movements
            WHERE movement_type IN ('STOCK_IN', 'STOCK_OUT')
            ORDER BY handling_unit, timestamp_utc DESC, id DESC
        )
        SELECT
            m.handling_unit,
            CASE
                WHEN m.last_movement_type = 'STOCK_IN'  THEN 'IN_WAREHOUSE'
                WHEN m.last_movement_type = 'STOCK_OUT' THEN 'OUT_OF_WAREHOUSE'
                ELSE 'UNKNOWN'
            END                                                         AS current_status,
            m.last_event_id,
            m.last_movement_type,
            m.last_movement_time AT TIME ZONE 'Asia/Dubai'             AS last_movement_time_dubai,
            COALESCE(NULLIF(m.indus_part,  ''), ps.indus_part)         AS indus_part,
            COALESCE(NULLIF(m.pallet_qty::text, ''), ps.pallet_qty::text) AS pallet_qty,
            COALESCE(NULLIF(m.location,    ''), ps.last_location)      AS last_location
        FROM latest_movement m
        LEFT JOIN pallet_status ps USING (handling_unit)
        {outer_where}
        ORDER BY m.last_movement_time DESC NULLS LAST
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
    qty = STOCK_QTY_EXPR
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
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
                    ORDER BY timestamp_utc DESC, id DESC
                    LIMIT %(limit)s
                """, {'limit': limit})
                movement_errors = [_row_to_dict(r) for r in cur.fetchall()]

                cur.execute(f"""
                    SELECT
                        COALESCE(NULLIF(indus_part,''), 'Unknown') AS indus_part,
                        COALESCE(NULLIF(location,''), 'Unknown')   AS location,
                        SUM(CASE WHEN movement_type='STOCK_IN'  THEN {qty} ELSE 0 END) AS total_in,
                        SUM(CASE WHEN movement_type='STOCK_OUT' THEN {qty} ELSE 0 END) AS total_out,
                        SUM(CASE WHEN movement_type='STOCK_IN'  THEN {qty} ELSE 0 END)
                        - SUM(CASE WHEN movement_type='STOCK_OUT' THEN {qty} ELSE 0 END) AS net_qty
                    FROM movements
                    WHERE movement_type IN ('STOCK_IN','STOCK_OUT')
                    GROUP BY indus_part, location
                    HAVING
                        SUM(CASE WHEN movement_type='STOCK_IN'  THEN {qty} ELSE 0 END)
                      - SUM(CASE WHEN movement_type='STOCK_OUT' THEN {qty} ELSE 0 END) < 0
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
    now = time.time()
    if _stats_cache['data'] is not None and (now - _stats_cache['ts']) < _STATS_CACHE_TTL:
        return jsonify(_stats_cache['data'])
    result = {
        "total_movements": None,
        "total_errors":    None,
        "pallets_in":      None,
        "pallets_out":     None,
        "override_count":  None,
        "pending_keys":    None,
        "ivt_today":       None,
        "ivt_total":       None,
        "bdr_today":       None,
        "bdr_total":       None,
        "neg_alerts":      None,
    }
    qty = STOCK_QTY_EXPR

    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:

                try:
                    cur.execute("SELECT COUNT(*) FROM movements WHERE movement_type != 'ERROR'")
                    result["total_movements"] = _fetch_scalar(cur)
                except Exception as e:
                    logger.warning("stats total_movements: %s", e)

                try:
                    cur.execute("""
                        WITH latest AS (
                            SELECT DISTINCT ON (handling_unit) movement_type
                            FROM movements
                            WHERE movement_type IN ('STOCK_IN','STOCK_OUT')
                            ORDER BY handling_unit, timestamp_utc DESC, id DESC
                        )
                        SELECT
                            SUM(CASE WHEN movement_type='STOCK_IN'  THEN 1 ELSE 0 END) AS cnt_in,
                            SUM(CASE WHEN movement_type='STOCK_OUT' THEN 1 ELSE 0 END) AS cnt_out
                        FROM latest
                    """)
                    row = cur.fetchone()
                    if row:
                        result["pallets_in"]  = _safe_int(row['cnt_in'])
                        result["pallets_out"] = _safe_int(row['cnt_out'])
                except Exception as e:
                    logger.warning("stats pallets: %s", e)

                try:
                    cur.execute("SELECT COUNT(*) FROM movements WHERE validation_status='OVERRIDE'")
                    result["override_count"] = _fetch_scalar(cur)
                except Exception as e:
                    logger.warning("stats overrides: %s", e)

                try:
                    cur.execute("""
                        SELECT COUNT(*) FROM movements
                        WHERE movement_type='ERROR'
                           OR validation_status NOT IN ('VALID','OVERRIDE')
                    """)
                    result["total_errors"] = _fetch_scalar(cur)
                except Exception as e:
                    logger.warning("stats errors: %s", e)

                try:
                    cur.execute("""
                        SELECT COUNT(*) FROM ivt_scans
                        WHERE DATE(scanned_date::timestamptz AT TIME ZONE 'Asia/Dubai') = CURRENT_DATE
                    """)
                    result["ivt_today"] = _fetch_scalar(cur)
                except Exception as e:
                    logger.warning("stats ivt_today: %s", e)

                try:
                    cur.execute("SELECT COUNT(*) FROM ivt_scans")
                    result["ivt_total"] = _fetch_scalar(cur)
                except Exception as e:
                    logger.warning("stats ivt_total: %s", e)

                # BDR stats — gracefully skipped if table doesn't exist yet
                try:
                    cur.execute("""
                        SELECT COUNT(*) FROM bdr_scans
                        WHERE DATE(scanned_date::timestamptz AT TIME ZONE 'Asia/Dubai') = CURRENT_DATE
                    """)
                    result["bdr_today"] = _fetch_scalar(cur)
                except Exception as e:
                    logger.warning("stats bdr_today: %s", e)
                    result["bdr_today"] = 0

                try:
                    cur.execute("SELECT COUNT(*) FROM bdr_scans")
                    result["bdr_total"] = _fetch_scalar(cur)
                except Exception as e:
                    logger.warning("stats bdr_total: %s", e)
                    result["bdr_total"] = 0

                try:
                    cur.execute(f"""
                        SELECT COUNT(*) FROM (
                            SELECT indus_part, location
                            FROM movements
                            WHERE movement_type IN ('STOCK_IN','STOCK_OUT')
                            GROUP BY indus_part, location
                            HAVING
                                SUM(CASE WHEN movement_type='STOCK_IN'  THEN {qty} ELSE 0 END)
                              - SUM(CASE WHEN movement_type='STOCK_OUT' THEN {qty} ELSE 0 END) < 0
                        ) t
                    """)
                    result["neg_alerts"] = _fetch_scalar(cur)
                except Exception as e:
                    logger.warning("stats neg_alerts: %s", e)

    except Exception as e:
        logger.error("api_stats DB connection failed: %s", e)

    try:
        result["pending_keys"] = pending_count()
    except Exception as e:
        logger.warning("stats pending_keys: %s", e)
        result["pending_keys"] = 0

    _stats_cache['data'] = result
    _stats_cache['ts'] = time.time()
    return jsonify(result)


@admin_bp.route('/api/connection-test')
@_auth_required
def api_conn_test():
    ok = db_ping()
    return jsonify({"status": "ok" if ok else "error", "db": ok})


# ---------------------------------------------------------------------------
# BDR scans endpoint
# ---------------------------------------------------------------------------

@admin_bp.route('/api/bdr-scans')
@_auth_required
def api_bdr_scans():
    """
    Returns all BDR scan records with decoded label fields.

    Query params:
      part     — filter by part_code (ILIKE)
      location — filter by location (exact, case-insensitive)
      limit    — max rows (default 500)

    Response:
      {
        "scans": [
          {
            "id": 1,
            "code": "BDR783832304 | 333536 | 26198 | ABC123 | T1",
            "customer_code": "783832304",
            "part_code": "333536",
            "year": "26",
            "day_of_year": "198",
            "serial": "ABC123",
            "location": "M4",
            "scanned_date": "2026-07-17T13:45:00+04:00"
          }, ...
        ],
        "total": 42
      }
    """
    import re
    part     = (request.args.get('part')     or '').strip()
    loc      = (request.args.get('location') or '').strip()
    limit    = request.args.get('limit', 500, type=int)

    clauses = []
    params  = {'limit': limit}

    if part:
        clauses.append("part_code ILIKE %(part)s")
        params['part'] = f'%{part}%'
    if loc and loc.upper() != 'ALL':
        clauses.append("UPPER(location) = UPPER(%(loc)s)")
        params['loc'] = loc

    where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''

    sql = f"""
        SELECT id, code, part_code, location,
               scanned_date AT TIME ZONE 'Asia/Dubai' AS scanned_date_dubai
        FROM bdr_scans
        {where}
        ORDER BY id DESC
        LIMIT %(limit)s
    """
    count_sql = f"SELECT COUNT(*) FROM bdr_scans {where}"

    # BDR label pattern: BDR{9-digit} | {part} | {YYDDD} | {serial} | T1
    _BDR_RE = re.compile(
        r'^BDR(\d{9})\s*\|\s*(\w+)\s*\|\s*(\d{2})(\d{3})\s*\|\s*(\w{6})\s*\|\s*T1$',
        re.IGNORECASE
    )

    def _decode(code):
        m = _BDR_RE.match((code or '').strip())
        if m:
            return {
                'customer_code': m.group(1),
                'part_code':     m.group(2),
                'year':          m.group(3),
                'day_of_year':   m.group(4),
                'serial':        m.group(5),
            }
        return {'customer_code': '', 'part_code': '', 'year': '', 'day_of_year': '', 'serial': ''}

    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                # count (uses same WHERE, no LIMIT)
                count_params = {k: v for k, v in params.items() if k != 'limit'}
                cur.execute(count_sql, count_params)
                total = _fetch_scalar(cur)

                cur.execute(sql, params)
                rows = []
                for r in cur.fetchall():
                    d = dict(r)
                    decoded = _decode(d.get('code', ''))
                    # part_code column is stored directly; enrich with full decode
                    d['customer_code'] = decoded['customer_code']
                    d['year']          = decoded['year']
                    d['day_of_year']   = decoded['day_of_year']
                    d['serial']        = decoded['serial']
                    # normalise date to ISO string
                    sd = d.pop('scanned_date_dubai', None)
                    d['scanned_date'] = sd.isoformat() if sd else None
                    rows.append(d)

        return jsonify({"scans": rows, "total": total})
    except Exception as e:
        logger.error("api_bdr_scans: %s", e)
        return jsonify({"scans": [], "total": 0, "error": str(e)})


@admin_bp.route('/api/analytics/bdr')
@_auth_required
def api_bdr_analytics():
    """
    Returns BDR scan analytics:
      - recent: last N scans
      - daily: scan count per day over last 14 days
      - by_location: scan count by location
      - by_part: scan count by part_code
    """
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT code, part_code, location,
                           scanned_date AT TIME ZONE 'Asia/Dubai' AS scanned_date
                    FROM bdr_scans ORDER BY id DESC LIMIT 200
                """)
                recent = [dict(r) for r in cur.fetchall()]

                cur.execute("""
                    SELECT TO_CHAR(scanned_date::timestamptz AT TIME ZONE 'Asia/Dubai','YYYY-MM-DD') AS day,
                           COUNT(*) AS cnt
                    FROM bdr_scans
                    WHERE scanned_date::timestamptz >= NOW() - INTERVAL '14 days'
                    GROUP BY day ORDER BY day
                """)
                daily = [dict(r) for r in cur.fetchall()]

                cur.execute("""
                    SELECT UPPER(location) AS location, COUNT(*) AS cnt
                    FROM bdr_scans
                    GROUP BY UPPER(location)
                    ORDER BY cnt DESC
                """)
                by_location = [dict(r) for r in cur.fetchall()]

                cur.execute("""
                    SELECT UPPER(part_code) AS part_code, COUNT(*) AS cnt
                    FROM bdr_scans
                    GROUP BY UPPER(part_code)
                    ORDER BY cnt DESC
                """)
                by_part = [dict(r) for r in cur.fetchall()]

        return jsonify({
            "recent": recent,
            "daily": daily,
            "by_location": by_location,
            "by_part": by_part,
        })
    except Exception as e:
        logger.error("api_bdr_analytics: %s", e)
        return jsonify({"recent": [], "daily": [], "by_location": [], "by_part": []})


# ---------------------------------------------------------------------------
# Stock summary — pivoted: one row per indus_part, one column per location
# ---------------------------------------------------------------------------

@admin_bp.route('/api/stock-summary')
@_auth_required
def api_stock_summary():
    part = (request.args.get('part') or '').strip()
    loc  = (request.args.get('location') or '').strip()
    clauses = ["movement_type IN ('STOCK_IN','STOCK_OUT')"]
    params  = {}
    if part:
        clauses.append("indus_part ILIKE %(part)s")
        params['part'] = f'%{part}%'
    if loc and loc != 'ALL':
        clauses.append("location = %(loc)s")
        params['loc'] = loc
    where = 'WHERE ' + ' AND '.join(clauses)
    qty = STOCK_QTY_EXPR

    sql = f"""
        SELECT
            COALESCE(NULLIF(indus_part, ''), 'Unknown') AS indus_part,
            SUM(CASE WHEN UPPER(location)='M4'
                     THEN (CASE WHEN movement_type='STOCK_IN' THEN {qty} ELSE -{qty} END)
                     ELSE 0 END) AS m4_stock,
            SUM(CASE WHEN UPPER(location)='M5'
                     THEN (CASE WHEN movement_type='STOCK_IN' THEN {qty} ELSE -{qty} END)
                     ELSE 0 END) AS m5_stock,
            SUM(CASE WHEN UPPER(location)='M6'
                     THEN (CASE WHEN movement_type='STOCK_IN' THEN {qty} ELSE -{qty} END)
                     ELSE 0 END) AS m6_stock,
            SUM(CASE WHEN UPPER(location)='M4_STORE'
                     THEN (CASE WHEN movement_type='STOCK_IN' THEN {qty} ELSE -{qty} END)
                     ELSE 0 END) AS m4_store_stock,
            SUM(CASE WHEN UPPER(location)='M16_STORE'
                     THEN (CASE WHEN movement_type='STOCK_IN' THEN {qty} ELSE -{qty} END)
                     ELSE 0 END) AS m16_store_stock,
            SUM(CASE WHEN movement_type='STOCK_IN'  THEN {qty} ELSE 0 END)
            - SUM(CASE WHEN movement_type='STOCK_OUT' THEN {qty} ELSE 0 END) AS total_stock
        FROM movements
        {where}
        GROUP BY indus_part
        ORDER BY indus_part
    """
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()]

        loc_col_map = [
            ('M4',       'm4_stock'),
            ('M5',       'm5_stock'),
            ('M6',       'm6_stock'),
            ('M4_STORE', 'm4_store_stock'),
            ('M16_STORE','m16_store_stock'),
        ]
        neg = []
        for r in rows:
            for loc_name, col in loc_col_map:
                val = _safe_int(r.get(col))
                if val < 0:
                    neg.append({
                        "indus_part": r["indus_part"],
                        "location":   loc_name,
                        "net_qty":    val,
                    })

        return jsonify({"rows": rows, "negative_stock": neg})
    except Exception as e:
        logger.error("api_stock_summary: %s", e)
        return jsonify({"rows": [], "negative_stock": [], "error": str(e)})


# ---------------------------------------------------------------------------
# Total stock summary (all materials, across all locations combined)
# ---------------------------------------------------------------------------

@admin_bp.route('/api/total-stock')
@_auth_required
def api_total_stock():
    part = (request.args.get('part') or '').strip()
    clauses = ["movement_type IN ('STOCK_IN','STOCK_OUT')"]
    params  = {}
    if part:
        clauses.append("indus_part ILIKE %(part)s")
        params['part'] = f'%{part}%'
    where = 'WHERE ' + ' AND '.join(clauses)
    qty = STOCK_QTY_EXPR
    sql = f"""
        SELECT
            COALESCE(NULLIF(indus_part,''),   'Unknown') AS indus_part,
            COALESCE(NULLIF(MAX(customer_part),''),'Unknown') AS customer_part,
            SUM(CASE WHEN movement_type='STOCK_IN'  THEN {qty} ELSE 0 END) AS total_in_qty,
            SUM(CASE WHEN movement_type='STOCK_OUT' THEN {qty} ELSE 0 END) AS total_out_qty,
            SUM(CASE WHEN movement_type='STOCK_IN'  THEN {qty} ELSE 0 END)
            - SUM(CASE WHEN movement_type='STOCK_OUT' THEN {qty} ELSE 0 END) AS net_qty,
            COUNT(DISTINCT CASE WHEN movement_type='STOCK_IN'  THEN handling_unit END) AS pallets_in,
            COUNT(DISTINCT CASE WHEN movement_type='STOCK_OUT' THEN handling_unit END) AS pallets_out,
            COUNT(DISTINCT location) AS location_count
        FROM movements
        {where}
        GROUP BY indus_part
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
# SAP Recon status — lightweight polling endpoint (no DB query, no SAP call)
# ---------------------------------------------------------------------------

@admin_bp.route('/api/sap-recon-status')
@_auth_required
def api_sap_recon_status():
    from app.services.sap_stock_client import (
        is_fetching, cache_age_seconds, last_error, fetch_started,
    )
    from app.services import sap_stock_client as _ssc

    fetching   = is_fetching()
    started    = fetch_started()
    data       = _ssc._cache.get("data")
    has_data   = data is not None and len(data) > 0
    mat_count  = len(data) if has_data else 0
    age        = round(cache_age_seconds())
    err        = last_error() or None

    return jsonify({
        "is_fetching":       fetching,
        "fetch_started":     started,
        "has_data":          has_data,
        "material_count":    mat_count,
        "cache_age_seconds": age,
        "error":             err,
    })


# ---------------------------------------------------------------------------
# SAP Reconciliation endpoint
# ---------------------------------------------------------------------------

@admin_bp.route('/api/sap-recon')
@_auth_required
def api_sap_recon():
    from app.services.sap_stock_client import (
        fetch_stock, cache_age_seconds, last_error, SAPStockError
    )

    force = request.args.get('force_refresh', '0') == '1'

    sap_rows  = []
    sap_error = None
    try:
        sap_rows = fetch_stock(force_refresh=force)
    except SAPStockError as exc:
        sap_error = str(exc)
        logger.error("api_sap_recon SAP fetch failed: %s", exc)

    sap_map = {
        r['material']: r
        for r in sap_rows
    }

    qty = STOCK_QTY_EXPR
    _part_expr = "UPPER(COALESCE(NULLIF(indus_part,''), 'Unknown'))"
    scanner_sql = f"""
        SELECT
            {_part_expr} AS indus_part,
            COALESCE(NULLIF(MAX(customer_part),''), '') AS customer_part,
            SUM(CASE WHEN movement_type='STOCK_IN'  THEN {qty} ELSE 0 END)
            - SUM(CASE WHEN movement_type='STOCK_OUT' THEN {qty} ELSE 0 END) AS net_qty
        FROM movements
        WHERE movement_type IN ('STOCK_IN','STOCK_OUT')
        GROUP BY {_part_expr}
        ORDER BY {_part_expr}
    """
    scanner_map = {}
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(scanner_sql)
                for row in cur.fetchall():
                    d = dict(row)
                    scanner_map[d['indus_part']] = d
    except Exception as e:
        logger.error("api_sap_recon scanner query failed: %s", e)
        return jsonify({"rows": [], "error": str(e)}), 500

    all_materials = sorted(set(sap_map.keys()) | set(scanner_map.keys()))

    rows = []
    for mat in all_materials:
        sap  = sap_map.get(mat, {})
        scan = scanner_map.get(mat, {})

        scanner_qty   = _safe_int(scan.get('net_qty', 0))
        sap_qty_1000  = round(float(sap.get('sap_qty_1000', 0) or 0))
        sap_qty_2000  = round(float(sap.get('sap_qty_2000', 0) or 0))
        variance      = scanner_qty - sap_qty_1000

        rows.append({
            'indus_part':       mat,
            'customer_part':    scan.get('customer_part', ''),
            'scanner_qty':      scanner_qty,
            'sap_qty_1000':     sap_qty_1000,
            'sap_qty_2000':     sap_qty_2000,
            'variance_vs_1000': variance,
        })

    return jsonify({
        'rows':                   rows,
        'cache_age_seconds':      round(cache_age_seconds()),
        'sap_material_count':     len(sap_map),
        'scanner_material_count': len(scanner_map),
        'error':                  sap_error,
    })


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------

@admin_bp.route('/api/export')
@_auth_required
def api_export():
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return Response(
            "openpyxl is not installed. Add 'openpyxl>=3.1.2' to requirements.txt.",
            status=500, mimetype='text/plain'
        )

    sheet_name = (request.args.get('sheet') or 'movements').lower()
    wb = openpyxl.Workbook()
    ws = wb.active

    # ---- Styling constants ------------------------------------------------
    HDR_FILL  = PatternFill('solid', fgColor='1E3A5F')
    HDR_FONT  = Font(bold=True, color='FFFFFF', size=10, name='Calibri')
    HDR_ALIGN = Alignment(horizontal='center', vertical='center', wrap_text=True)
    DATA_FONT  = Font(size=10, name='Calibri')
    DATA_ALIGN = Alignment(vertical='center')
    _thin = Side(style='thin', color='CCCCCC')
    CELL_BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)

    def _apply_header(ws, num_cols, row_height=22):
        ws.row_dimensions[1].height = row_height
        for cell in ws[1]:
            cell.fill      = HDR_FILL
            cell.font      = HDR_FONT
            cell.alignment = HDR_ALIGN
            cell.border    = CELL_BORDER
        for j in range(1, num_cols + 1):
            ws.column_dimensions[get_column_letter(j)].width = 22

    def _write_rows(ws, headers, data, start_row=2):
        for i, row in enumerate(data, start=start_row):
            ws.row_dimensions[i].height = 18
            for j, h in enumerate(headers, start=1):
                val = row.get(h)
                if h in _INT_EXPORT_COLS:
                    cell_val = _safe_int(val, default=0)
                elif h in _DT_EXPORT_COLS:
                    try:
                        cell_val = datetime.strptime(str(val), _DT_PARSE_FMT)
                    except (ValueError, TypeError):
                        cell_val = str(val) if val is not None else ''
                else:
                    cell_val = str(val) if val is not None else ''

                cell = ws.cell(row=i, column=j, value=cell_val)
                cell.font      = DATA_FONT
                cell.alignment = DATA_ALIGN
                cell.border    = CELL_BORDER
                if h in _DT_EXPORT_COLS and isinstance(cell_val, datetime):
                    cell.number_format = _DT_EXPORT_FMT

    def _get_oor_codes_safe():
        now = time.time()
        if _oor_cache['data'] is not None and (now - _oor_cache['ts']) < _OOR_CACHE_TTL:
            return _oor_cache['data']
        try:
            codes = _fetch_oor_customer_codes()
            _oor_cache['data'] = codes
            _oor_cache['ts']   = now
            return codes
        except Exception as exc:
            logger.warning("export: OOR lookup failed, customer column will be blank: %s", exc)
            return _oor_cache['data'] or {}

    qty = STOCK_QTY_EXPR

    try:
        if sheet_name == 'movements':
            clauses, params = _build_movement_filters(request.args)
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
                        ORDER BY timestamp_utc DESC
                    """, params)
                    data = [_row_to_dict(r) for r in cur.fetchall()]

            oor = _get_oor_codes_safe()
            for row in data:
                part_key = (row.get('indus_part') or '').strip().upper()
                row['customer'] = oor.get(part_key, '')

            ws.title = 'Movements'
            headers = ['event_id','timestamp_dubai','handling_unit','indus_part',
                       'customer','customer_part',
                       'pallet_qty','job_no','movement_type','operator_id','device_id',
                       'sync_status','validation_status','location','from_location','to_location']
            ws.append(headers)
            _apply_header(ws, len(headers))
            _write_rows(ws, headers, data)
            filename = 'movements_export.xlsx'

        elif sheet_name == 'statuses':
            hu   = (request.args.get('hu')   or '').strip()
            part = (request.args.get('part') or '').strip()
            loc  = (request.args.get('location') or '').strip()
            outer_clauses, p2 = [], {}
            if hu:
                outer_clauses.append("m.handling_unit ILIKE %(hu)s")
                p2['hu'] = f'%{hu}%'
            if part:
                outer_clauses.append("(COALESCE(m.indus_part, ps.indus_part)) ILIKE %(part)s")
                p2['part'] = f'%{part}%'
            if loc and loc != 'ALL':
                outer_clauses.append(
                    "COALESCE(NULLIF(m.location,''), ps.last_location) = %(loc)s"
                )
                p2['loc'] = loc
            outer_where2 = ('WHERE ' + ' AND '.join(outer_clauses)) if outer_clauses else ''
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        WITH latest_movement AS (
                            SELECT DISTINCT ON (handling_unit)
                                handling_unit,
                                movement_type AS last_movement_type,
                                timestamp_utc AS last_movement_time,
                                location,
                                indus_part,
                                pallet_qty,
                                event_id AS last_event_id
                            FROM movements
                            WHERE movement_type IN ('STOCK_IN', 'STOCK_OUT')
                            ORDER BY handling_unit, timestamp_utc DESC, id DESC
                        )
                        SELECT
                            m.handling_unit,
                            CASE
                                WHEN m.last_movement_type = 'STOCK_IN'  THEN 'IN_WAREHOUSE'
                                WHEN m.last_movement_type = 'STOCK_OUT' THEN 'OUT_OF_WAREHOUSE'
                                ELSE 'UNKNOWN'
                            END AS current_status,
                            m.last_movement_type,
                            m.last_movement_time AT TIME ZONE 'Asia/Dubai' AS last_movement_time_dubai,
                            COALESCE(NULLIF(m.indus_part,''), ps.indus_part) AS indus_part,
                            COALESCE(NULLIF(m.pallet_qty::text,''), ps.pallet_qty::text) AS pallet_qty,
                            COALESCE(NULLIF(m.location,''), ps.last_location) AS last_location
                        FROM latest_movement m
                        LEFT JOIN pallet_status ps USING (handling_unit)
                        {outer_where2}
                        ORDER BY m.last_movement_time DESC NULLS LAST
                    """, p2)
                    data = [_row_to_dict(r) for r in cur.fetchall()]

            oor = _get_oor_codes_safe()
            for row in data:
                part_key = (row.get('indus_part') or '').strip().upper()
                row['customer'] = oor.get(part_key, '')

            ws.title = 'Pallet Status'
            headers = ['handling_unit','current_status','last_movement_type',
                       'last_movement_time_dubai','indus_part','customer',
                       'pallet_qty','last_location']
            ws.append(headers)
            _apply_header(ws, len(headers))
            _write_rows(ws, headers, data)
            filename = 'pallet_status_export.xlsx'

        elif sheet_name == 'stock':
            part = (request.args.get('part') or '').strip()
            loc  = (request.args.get('location') or '').strip()
            cl3 = ["movement_type IN ('STOCK_IN','STOCK_OUT')"]
            p3  = {}
            if part: cl3.append("indus_part ILIKE %(part)s"); p3['part'] = f'%{part}%'
            if loc and loc != 'ALL': cl3.append("location=%(loc)s"); p3['loc'] = loc
            where3 = 'WHERE ' + ' AND '.join(cl3)
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT
                            COALESCE(NULLIF(indus_part,''),'Unknown') AS indus_part,
                            SUM(CASE WHEN UPPER(location)='M4'
                                     THEN (CASE WHEN movement_type='STOCK_IN' THEN {qty} ELSE -{qty} END)
                                     ELSE 0 END) AS m4_stock,
                            SUM(CASE WHEN UPPER(location)='M5'
                                     THEN (CASE WHEN movement_type='STOCK_IN' THEN {qty} ELSE -{qty} END)
                                     ELSE 0 END) AS m5_stock,
                            SUM(CASE WHEN UPPER(location)='M6'
                                     THEN (CASE WHEN movement_type='STOCK_IN' THEN {qty} ELSE -{qty} END)
                                     ELSE 0 END) AS m6_stock,
                            SUM(CASE WHEN UPPER(location)='M4_STORE'
                                     THEN (CASE WHEN movement_type='STOCK_IN' THEN {qty} ELSE -{qty} END)
                                     ELSE 0 END) AS m4_store_stock,
                            SUM(CASE WHEN UPPER(location)='M16_STORE'
                                     THEN (CASE WHEN movement_type='STOCK_IN' THEN {qty} ELSE -{qty} END)
                                     ELSE 0 END) AS m16_store_stock,
                            SUM(CASE WHEN movement_type='STOCK_IN'  THEN {qty} ELSE 0 END)
                            - SUM(CASE WHEN movement_type='STOCK_OUT' THEN {qty} ELSE 0 END) AS total_stock
                        FROM movements {where3}
                        GROUP BY indus_part
                        ORDER BY indus_part
                    """, p3)
                    data = [dict(r) for r in cur.fetchall()]

            oor = _get_oor_codes_safe()
            for row in data:
                part_key = (row.get('indus_part') or '').strip().upper()
                row['customer'] = oor.get(part_key, '')

            ws.title = 'Stock by Location'
            headers = ['indus_part','customer',
                       'm4_stock','m5_stock','m6_stock',
                       'm4_store_stock','m16_store_stock','total_stock']
            ws.append(headers)
            _apply_header(ws, len(headers))
            _write_rows(ws, headers, data)
            filename = 'stock_by_location_export.xlsx'

        elif sheet_name == 'total_stock':
            part = (request.args.get('part') or '').strip()
            cl4 = ["movement_type IN ('STOCK_IN','STOCK_OUT')"]
            p4  = {}
            if part:
                cl4.append("indus_part ILIKE %(part)s")
                p4['part'] = f'%{part}%'
            where4 = 'WHERE ' + ' AND '.join(cl4)
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT
                            COALESCE(NULLIF(indus_part,''),'Unknown')         AS indus_part,
                            COALESCE(NULLIF(MAX(customer_part),''),'Unknown') AS customer_part,
                            SUM(CASE WHEN movement_type='STOCK_IN'  THEN {qty} ELSE 0 END) AS total_in_qty,
                            SUM(CASE WHEN movement_type='STOCK_OUT' THEN {qty} ELSE 0 END) AS total_out_qty,
                            SUM(CASE WHEN movement_type='STOCK_IN'  THEN {qty} ELSE 0 END)
                            - SUM(CASE WHEN movement_type='STOCK_OUT' THEN {qty} ELSE 0 END) AS net_qty,
                            COUNT(DISTINCT CASE WHEN movement_type='STOCK_IN'  THEN handling_unit END) AS pallets_in,
                            COUNT(DISTINCT CASE WHEN movement_type='STOCK_OUT' THEN handling_unit END) AS pallets_out,
                            COUNT(DISTINCT location) AS location_count
                        FROM movements {where4}
                        GROUP BY indus_part
                        ORDER BY indus_part
                    """, p4)
                    data = [dict(r) for r in cur.fetchall()]

            oor = _get_oor_codes_safe()
            for row in data:
                part_key = (row.get('indus_part') or '').strip().upper()
                row['customer'] = oor.get(part_key, '')

            ws.title = 'Total Stock'
            headers = ['indus_part','customer','customer_part',
                       'total_in_qty','total_out_qty','net_qty',
                       'pallets_in','pallets_out','location_count']
            ws.append(headers)
            _apply_header(ws, len(headers))
            _write_rows(ws, headers, data)
            filename = 'total_stock_export.xlsx'

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
                    cur.execute(f"""
                        SELECT
                            COALESCE(NULLIF(indus_part,''),'Unknown') AS indus_part,
                            COALESCE(NULLIF(location,''),'Unknown')   AS location,
                            SUM(CASE WHEN movement_type='STOCK_IN'  THEN {qty} ELSE 0 END) AS total_in,
                            SUM(CASE WHEN movement_type='STOCK_OUT' THEN {qty} ELSE 0 END) AS total_out,
                            SUM(CASE WHEN movement_type='STOCK_IN'  THEN {qty} ELSE 0 END)
                            - SUM(CASE WHEN movement_type='STOCK_OUT' THEN {qty} ELSE 0 END) AS net_qty
                        FROM movements
                        WHERE movement_type IN ('STOCK_IN','STOCK_OUT')
                        GROUP BY indus_part, location
                        HAVING
                            SUM(CASE WHEN movement_type='STOCK_IN'  THEN {qty} ELSE 0 END)
                          - SUM(CASE WHEN movement_type='STOCK_OUT' THEN {qty} ELSE 0 END) < 0
                        ORDER BY net_qty
                    """)
                    neg = [dict(r) for r in cur.fetchall()]

            ws.title = 'Movement Errors'
            h1 = ['event_id','timestamp_dubai','handling_unit','movement_type',
                  'validation_status','operator_id','location','sync_status']
            ws.append(h1)
            _apply_header(ws, len(h1))
            _write_rows(ws, h1, mov_errors)

            ws2 = wb.create_sheet('Negative Stock')
            h2 = ['indus_part','location','total_in','total_out','net_qty']
            ws2.append(h2)
            _apply_header(ws2, len(h2))
            _write_rows(ws2, h2, neg)
            filename = 'error_log_export.xlsx'

        elif sheet_name == 'ivt':
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT code, scanned_date, location FROM ivt_scans ORDER BY id DESC")
                    data = [dict(r) for r in cur.fetchall()]
            ws.title = 'IVT Scans'
            headers = ['code', 'scanned_date', 'location']
            ws.append(headers)
            _apply_header(ws, len(headers))
            _write_rows(ws, headers, data)
            filename = 'ivt_scans_export.xlsx'

        elif sheet_name == 'bdr':
            # BDR scans export — applies same part/location filters as api_bdr_scans
            import re as _re
            _BDR_RE = _re.compile(
                r'^BDR(\d{9})\s*\|\s*(\w+)\s*\|\s*(\d{2})(\d{3})\s*\|\s*(\w{6})\s*\|\s*T1$',
                _re.IGNORECASE
            )

            part_f = (request.args.get('part')     or '').strip()
            loc_f  = (request.args.get('location') or '').strip()
            cl_bdr = []
            p_bdr  = {}
            if part_f:
                cl_bdr.append("part_code ILIKE %(part)s")
                p_bdr['part'] = f'%{part_f}%'
            if loc_f and loc_f.upper() != 'ALL':
                cl_bdr.append("UPPER(location) = UPPER(%(loc)s)")
                p_bdr['loc'] = loc_f
            where_bdr = ('WHERE ' + ' AND '.join(cl_bdr)) if cl_bdr else ''

            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT id, code, part_code, location,
                               scanned_date AT TIME ZONE 'Asia/Dubai' AS scanned_date
                        FROM bdr_scans {where_bdr}
                        ORDER BY id DESC
                    """, p_bdr)
                    raw = [dict(r) for r in cur.fetchall()]

            export_rows = []
            for r in raw:
                m = _BDR_RE.match((r.get('code') or '').strip())
                export_rows.append({
                    'id':            r.get('id', ''),
                    'code':          r.get('code', ''),
                    'customer_code': m.group(1) if m else '',
                    'part_code':     r.get('part_code', ''),
                    'year':          m.group(3) if m else '',
                    'day_of_year':   m.group(4) if m else '',
                    'serial':        m.group(5) if m else '',
                    'location':      r.get('location', ''),
                    'scanned_date':  str(r.get('scanned_date', '')) if r.get('scanned_date') else '',
                })

            ws.title = 'BDR Scans'
            headers = ['id','code','customer_code','part_code',
                       'year','day_of_year','serial','location','scanned_date']
            ws.append(headers)
            _apply_header(ws, len(headers))
            _write_rows(ws, headers, export_rows)
            filename = 'bdr_scans_export.xlsx'

        elif sheet_name == 'sap_recon':
            from app.services.sap_stock_client import fetch_stock, SAPStockError
            sap_map = {}
            try:
                for r in fetch_stock(force_refresh=False):
                    sap_map[r['material']] = r
            except SAPStockError as exc:
                logger.warning("sap_recon export: SAP fetch failed: %s", exc)

            _part_expr = "UPPER(COALESCE(NULLIF(indus_part,''),'Unknown'))"
            scanner_data = []
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT
                            {_part_expr} AS indus_part,
                            COALESCE(NULLIF(MAX(customer_part),''),'') AS customer_part,
                            SUM(CASE WHEN movement_type='STOCK_IN'  THEN {qty} ELSE 0 END)
                            - SUM(CASE WHEN movement_type='STOCK_OUT' THEN {qty} ELSE 0 END) AS net_qty
                        FROM movements
                        WHERE movement_type IN ('STOCK_IN','STOCK_OUT')
                        GROUP BY {_part_expr}
                        ORDER BY {_part_expr}
                    """)
                    scanner_data = [dict(r) for r in cur.fetchall()]

            scanner_map = {r['indus_part']: r for r in scanner_data}
            all_mats = sorted(set(sap_map.keys()) | set(scanner_map.keys()))

            oor = _get_oor_codes_safe()

            export_rows = []
            for mat in all_mats:
                sap  = sap_map.get(mat, {})
                scan = scanner_map.get(mat, {})
                sq   = _safe_int(scan.get('net_qty', 0))
                s1   = round(float(sap.get('sap_qty_1000', 0) or 0))
                s2   = round(float(sap.get('sap_qty_2000', 0) or 0))
                export_rows.append({
                    'indus_part':       mat,
                    'customer':         oor.get(mat, ''),
                    'customer_part':    scan.get('customer_part', ''),
                    'scanner_qty':      sq,
                    'sap_qty_1000':     s1,
                    'sap_qty_2000':     s2,
                    'variance_vs_1000': sq - s1,
                })

            search_q   = (request.args.get('search') or '').strip().lower()
            var_filter = (request.args.get('variance_filter') or 'ALL').strip().upper()

            if search_q:
                export_rows = [
                    r for r in export_rows
                    if search_q in (r['indus_part'] or '').lower()
                    or search_q in (r['customer_part'] or '').lower()
                    or search_q in (r['customer'] or '').lower()
                ]
            if var_filter == 'DISC':
                export_rows = [r for r in export_rows if r['variance_vs_1000'] != 0]
            elif var_filter == 'NEG':
                export_rows = [r for r in export_rows if r['variance_vs_1000'] < 0]
            elif var_filter == 'EXCESS':
                export_rows = [r for r in export_rows if r['variance_vs_1000'] > 0]
            elif var_filter == 'SAP_ONLY':
                export_rows = [r for r in export_rows if r['scanner_qty'] == 0 and r['sap_qty_1000'] > 0]
            elif var_filter == 'SCAN_ONLY':
                export_rows = [r for r in export_rows if r['sap_qty_1000'] == 0 and r['scanner_qty'] > 0]

            ws.title = 'SAP Recon'
            headers = ['indus_part','customer','customer_part','scanner_qty',
                       'sap_qty_1000','sap_qty_2000','variance_vs_1000']
            ws.append(headers)
            _apply_header(ws, len(headers))
            _write_rows(ws, headers, export_rows)

            NEG_FILL = PatternFill('solid', fgColor='2D0A0A')
            NEG_FONT = Font(size=10, name='Calibri', color='FCA5A5')
            for i, row in enumerate(export_rows, start=2):
                if _safe_int(row.get('variance_vs_1000', 0)) < 0:
                    for j in range(1, len(headers) + 1):
                        ws.cell(row=i, column=j).fill = NEG_FILL
                        ws.cell(row=i, column=j).font = NEG_FONT

            filename = 'sap_recon_export.xlsx'

        else:
            return jsonify({"error": f"Unknown sheet: {sheet_name}"}), 400

    except Exception as e:
        logger.error("api_export sheet=%s: %s", sheet_name, e)
        return Response(f"Export failed: {e}", status=500, mimetype='text/plain')

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf, as_attachment=True, download_name=filename,
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
                    WITH latest AS (
                        SELECT DISTINCT ON (handling_unit)
                            handling_unit, location, movement_type
                        FROM movements
                        WHERE movement_type IN ('STOCK_IN','STOCK_OUT')
                        ORDER BY handling_unit, timestamp_utc DESC, id DESC
                    )
                    SELECT
                        COALESCE(NULLIF(location,''),'Unknown') AS location,
                        COUNT(*) AS pallet_count
                    FROM latest
                    WHERE movement_type = 'STOCK_IN'
                    GROUP BY location
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
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    WITH latest AS (
                        SELECT DISTINCT ON (handling_unit)
                            handling_unit,
                            movement_type,
                            indus_part
                        FROM movements
                        WHERE movement_type IN ('STOCK_IN','STOCK_OUT')
                        ORDER BY handling_unit, timestamp_utc DESC, id DESC
                    )
                    SELECT
                        COALESCE(NULLIF(indus_part,''),'Unknown') AS part,
                        COUNT(*) AS pallet_count
                    FROM latest
                    WHERE movement_type = 'STOCK_IN'
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
                    if isinstance(d.get('last_active'), datetime):
                        d['last_active'] = d['last_active'].strftime('%d %b %Y, %H:%M').lstrip('0')
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
                cur.execute("SELECT code, scanned_date, location FROM ivt_scans ORDER BY id DESC")
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
