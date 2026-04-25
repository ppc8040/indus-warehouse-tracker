"""
scripts/migrate_excel_to_pg.py
 
Migration script with enhanced timestamp parsing and type coercion.
Handles all 3 data corruption issues:
  1. Unparseable timestamp strings (274 rows) - missing seconds component
  2. Float Handling_Unit in scientific notation (2 rows) - precision loss  
  3. Integer Customer_Part values (889 rows) - wrong type
 
NOTE: No uniqueness check on event_id — all rows from Excel are inserted as-is.
Duplicate event_ids in the source Excel are intentionally preserved.
Going forward the application always generates unique event_ids.
 
Usage:
    heroku run python scripts/migrate_excel_to_pg.py -a indus-warehouse-tracker
    OR with --force to truncate first:
    heroku run python scripts/migrate_excel_to_pg.py --force -a indus-warehouse-tracker
"""
 
import os
import re
import sys
import time
import logging
import argparse
from datetime import datetime, timezone, timedelta
 
import msal
import requests
import psycopg2
import psycopg2.extras
 
try:
    from dateutil import parser as dateutil_parser
    _DATEUTIL_AVAILABLE = True
except ImportError:
    _DATEUTIL_AVAILABLE = False
 
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('migrate')
 
 
# ── Config ────────────────────────────────────────────────────────
 
GRAPH_TENANT_ID      = 'ed97e9bb-e119-4bd4-ab00-307d64bdf908'
GRAPH_CLIENT_ID      = 'a2239be7-12cd-442d-983f-ea7e316ef767'
GRAPH_CLIENT_SECRET  = 'sho8Q~sanlYEX3SKXjYJb0NSXL.gPRpdi63-tcmp'
SHAREPOINT_SITE_ID   = '201c767f-23cc-4eff-b76c-47a275706531,094b21f9-6ed4-47c3-a751-3b217a18b98e'
WORKBOOK_FILENAME    = 'Indus Warehouse Stock Movement.xlsx'
SHEET_MOVEMENT_LOG   = 'Movement_Log'
SHEET_IVT_LINE       = 'IVT_Line'
 
 
# ── Postgres connection ───────────────────────────────────────────
 
def get_pg_conn():
    uri = os.environ.get('DATABASE_URL')
    if not uri:
        log.error('DATABASE_URL is not set. Aborting.')
        sys.exit(1)
    if uri.startswith('postgres://'):
        uri = uri.replace('postgres://', 'postgresql://', 1)
    return psycopg2.connect(uri)
 
 
# ── Graph API helpers ──────────────────────────────────────────────
 
_http = requests.Session()
_token = {'value': None, 'expires_at': 0}
 
 
def _get_token():
    if _token['value'] and _token['expires_at'] > time.time() + 60:
        return _token['value']
    authority = f'https://login.microsoftonline.com/{GRAPH_TENANT_ID}'
    app = msal.ConfidentialClientApplication(
        GRAPH_CLIENT_ID,
        authority=authority,
        client_credential=GRAPH_CLIENT_SECRET
    )
    result = app.acquire_token_for_client(scopes=['https://graph.microsoft.com/.default'])
    if 'access_token' not in result:
        log.error(f"Auth failed: {result.get('error_description')}")
        sys.exit(1)
    _token['value'] = result['access_token']
    _token['expires_at'] = time.time() + result.get('expires_in', 3600)
    log.info('Graph token acquired.')
    return _token['value']
 
 
def _wb_url():
    return (f'https://graph.microsoft.com/v1.0/sites/{SHAREPOINT_SITE_ID}'
            f'/drive/root:/{WORKBOOK_FILENAME}:/workbook')
 
 
def _open_session():
    token = _get_token()
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    resp = _http.post(f'{_wb_url()}/createSession',
                      headers=headers,
                      json={'persistChanges': False},
                      timeout=30)
    if resp.status_code not in (200, 201):
        log.error(f'Could not open workbook session: {resp.text[:300]}')
        sys.exit(1)
    sid = resp.json()['id']
    log.info(f'Workbook session opened: {sid[:20]}...')
    return sid, {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'workbook-session-id': sid,
    }
 
 
def _close_session(sid):
    try:
        token = _get_token()
        _http.post(f'{_wb_url()}/closeSession',
                   headers={'Authorization': f'Bearer {token}', 'workbook-session-id': sid},
                   timeout=10)
        log.info('Workbook session closed.')
    except Exception as e:
        log.warning(f'Session close failed (non-fatal): {e}')
 
 
def _read_sheet(sheet_name, session_headers):
    url = f'{_wb_url()}/worksheets(\'{sheet_name}\')/usedRange'
    resp = _http.get(url, headers=session_headers, timeout=60)
    if resp.status_code == 404:
        log.warning(f"Sheet '{sheet_name}' not found — skipping.")
        return [], []
    if resp.status_code != 200:
        log.error(f"Failed to read '{sheet_name}': {resp.status_code} {resp.text[:200]}")
        return [], []
    values = resp.json().get('values', [])
    if not values:
        log.warning(f"Sheet '{sheet_name}' is empty.")
        return [], []
    headers = [str(v).strip() if v is not None else '' for v in values[0]]
    rows = values[1:] if len(values) > 1 else []
    log.info(f"Read {len(rows):,} data rows from '{sheet_name}'")
    return headers, rows
 
 
# ══════════════════════════════════════════════════════════════════
#  FIX #2: Handling_Unit Float → Text Conversion
# ══════════════════════════════════════════════════════════════════
 
def _clean_str(val):
    if val is None:
        return None
    if isinstance(val, float):
        if val > 1e16:
            try:
                return str(int(val))
            except (ValueError, OverflowError):
                log.warning(f"Float HU conversion failed for {val}, using str()")
                return str(val)
    s = str(val).strip()
    if s.startswith("'"):
        s = s[1:]
    return s if s else None
 
 
def _clean_int(val):
    if val is None:
        return None
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return None
 
 
# ══════════════════════════════════════════════════════════════════
#  FIX #3: Customer_Part Integer → Text Conversion
# ══════════════════════════════════════════════════════════════════
 
def _clean_customer_part(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if isinstance(val, float) and val.is_integer():
            return str(int(val))
        return str(int(val))
    return _clean_str(val)
 
 
# ══════════════════════════════════════════════════════════════════
#  FIX #1: Enhanced Timestamp Parsing
# ══════════════════════════════════════════════════════════════════
 
def _normalise_ts_string(raw):
    s = str(raw)
    s = s.replace('\u00a0', ' ')
    s = s.replace('\u200b', '')
    s = s.replace('\ufeff', '')
    s = s.replace('\u00ad', '')
    s = s.lstrip("'")
    s = ' '.join(s.split())
    return s.strip()
 
 
_TS_FORMATS = [
    '%d/%m/%Y %I:%M:%S %p',
    '%d/%m/%Y %H:%M:%S',
    '%d/%m/%Y %H:%M',
    '%d/%m/%Y %I:%M %p',
    '%m/%d/%Y %I:%M:%S %p',
    '%m/%d/%Y %H:%M:%S',
    '%m/%d/%Y %H:%M',
    '%m/%d/%Y %I:%M %p',
    '%Y-%m-%dT%H:%M:%S',
    '%Y-%m-%dT%H:%M:%SZ',
    '%Y-%m-%dT%H:%M:%S.%f',
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%d %H:%M',
    '%Y-%m-%d',
    '%d-%m-%Y %H:%M:%S',
    '%d-%m-%Y %H:%M',
    '%d %b %Y %H:%M:%S',
    '%d %B %Y %H:%M:%S',
    '%b %d, %Y %I:%M:%S %p',
]
 
_TS_REGEX = re.compile(
    r'(\d{1,4})[/\-\.](\d{1,2})[/\-\.](\d{2,4})'
    r'(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*(AM|PM))?)?',
    re.IGNORECASE
)
 
 
def _regex_fallback(s):
    m = _TS_REGEX.search(s)
    if not m:
        return None
    p1, p2, p3, hh, mm, ss, ampm = m.groups()
    p1, p2, p3 = int(p1), int(p2), int(p3)
    if p3 > 31:
        year, month, day = p3, p2, p1
    elif p1 > 31:
        year, month, day = p1, p2, p3
    else:
        day, month, year = p1, p2, p3
        if year < 100:
            year += 2000
    hour   = int(hh) if hh else 0
    minute = int(mm) if mm else 0
    second = int(ss) if ss else 0
    if ampm:
        ampm = ampm.upper()
        if ampm == 'PM' and hour != 12:
            hour += 12
        elif ampm == 'AM' and hour == 12:
            hour = 0
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        try:
            return datetime(year, day, month, hour, minute, second)
        except ValueError:
            return None
 
 
def _parse_timestamp(val):
    if val is None:
        return None
    s = _normalise_ts_string(val)
    if not s:
        return None
    try:
        serial = float(s)
        if serial > 1:
            base = datetime(1899, 12, 30, tzinfo=timezone.utc)
            return base + timedelta(days=serial)
    except ValueError:
        pass
    def _to_utc(dt_naive):
        return dt_naive.replace(tzinfo=None).replace(tzinfo=timezone.utc) - timedelta(hours=4)
    if _DATEUTIL_AVAILABLE:
        try:
            return _to_utc(dateutil_parser.parse(s, dayfirst=True))
        except (ValueError, OverflowError, TypeError):
            pass
    for fmt in _TS_FORMATS:
        try:
            return _to_utc(datetime.strptime(s, fmt))
        except ValueError:
            continue
    dt = _regex_fallback(s)
    if dt is not None:
        return _to_utc(dt)
    log.warning(f"Unparseable timestamp '{s}' — using current migration time as fallback")
    return datetime.now(timezone.utc)
 
 
# ══════════════════════════════════════════════════════════════════
#  Schema cleanup  (DDL must run outside any open transaction)
# ══════════════════════════════════════════════════════════════════

def _drop_event_id_unique_constraint(pg):
    """
    Drop the UNIQUE constraint on movements.event_id and its dependent FK.
    Must be called only when NO transaction is open on `pg`.
    We use a fresh connection with autocommit=True so the DDL commits
    immediately and is visible to the subsequent data transaction.
    """
    uri = os.environ.get('DATABASE_URL', '')
    if uri.startswith('postgres://'):
        uri = uri.replace('postgres://', 'postgresql://', 1)

    ddl_conn = psycopg2.connect(uri)
    ddl_conn.autocommit = True
    cur = ddl_conn.cursor()
    try:
        log.info('Dropping FK pallet_status_last_event_id_fkey (if present)...')
        cur.execute("""
            ALTER TABLE pallet_status
            DROP CONSTRAINT IF EXISTS pallet_status_last_event_id_fkey;
        """)

        log.info('Dropping UNIQUE constraint movements_event_id_key (if present)...')
        cur.execute("""
            ALTER TABLE movements
            DROP CONSTRAINT IF EXISTS movements_event_id_key CASCADE;
        """)

        log.info('Constraint cleanup complete.')
    finally:
        cur.close()
        ddl_conn.close()


# ══════════════════════════════════════════════════════════════════
#  Migration Functions
# ══════════════════════════════════════════════════════════════════
 
def migrate_movements(headers, rows, cur):
    COL = {h: i for i, h in enumerate(headers)}
 
    required = ['Event_ID', 'Timestamp_UTC', 'Handling_Unit', 'Movement_Type']
    missing = [c for c in required if c not in COL]
    if missing:
        log.error(f'Movement_Log is missing required columns: {missing}')
        log.error(f'Columns found: {headers}')
        sys.exit(1)
 
    def g(row, col, default=None):
        idx = COL.get(col)
        if idx is None or idx >= len(row):
            return default
        return row[idx]
 
    records = []
    skipped_no_id = 0
    skipped_bad_type = 0
    fallback_timestamps = 0
 
    for i, row in enumerate(rows, start=2):
        event_id = _clean_str(g(row, 'Event_ID'))
        if not event_id:
            skipped_no_id += 1
            continue
 
        movement_type = _clean_str(g(row, 'Movement_Type'))
        if movement_type not in ('STOCK_IN', 'STOCK_OUT'):
            log.warning(f'Row {i}: unknown movement_type "{movement_type}" — skipping')
            skipped_bad_type += 1
            continue
 
        raw_ts = g(row, 'Timestamp_UTC')
        timestamp = _parse_timestamp(raw_ts)
        if timestamp == datetime.now(timezone.utc):
            fallback_timestamps += 1
 
        customer_part_raw = g(row, 'Customer_Part')
        customer_part = _clean_customer_part(customer_part_raw)
        handling_unit_raw = g(row, 'Handling_Unit')
        handling_unit = _clean_str(handling_unit_raw)
 
        sync_status = _clean_str(g(row, 'Sync_Status')) or 'SYNCED'
        if sync_status not in ('SYNCED', 'PENDING'):
            sync_status = 'SYNCED'
 
        validation_status = _clean_str(g(row, 'Validation_Status')) or 'VALID'
        if validation_status not in ('VALID', 'OVERRIDE'):
            validation_status = 'VALID'
 
        records.append((
            event_id,
            timestamp,
            handling_unit,
            _clean_str(g(row, 'Indus_Part')),
            customer_part,
            _clean_int(g(row, 'Pallet_Qty')),
            _clean_str(g(row, 'Job_No')),
            movement_type,
            _clean_str(g(row, 'Operator_ID')),
            _clean_str(g(row, 'Device_ID')),
            sync_status,
            validation_status,
            _clean_str(g(row, 'Location')),
            None,
        ))
 
    log.info(
        f'Prepared {len(records):,} movement records '
        f'({skipped_no_id} no-id, {skipped_bad_type} bad-type, '
        f'{fallback_timestamps} fallback timestamps).'
    )
 
    if not records:
        log.error('No valid records to insert. Aborting.')
        sys.exit(1)
 
    # Plain INSERT — no ON CONFLICT clause.
    # All rows from Excel are inserted as-is, including duplicate event_ids
    # from historical data. Going forward the app always generates unique event_ids.
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO movements
            (event_id, timestamp_utc, handling_unit, indus_part, customer_part,
             pallet_qty, job_no, movement_type, operator_id, device_id,
             sync_status, validation_status, location, destination)
        VALUES %s
        """,
        records,
        page_size=500
    )
    log.info(f'Inserted {len(records):,} rows into movements (all rows, no dedup).')
    return len(records), 0
 
 
def derive_pallet_status(cur):
    log.info('Deriving pallet_status from movements...')
    cur.execute("""
        INSERT INTO pallet_status
            (handling_unit, current_status, last_event_id, last_movement_type,
             last_movement_time, indus_part, pallet_qty, last_location)
        SELECT
            handling_unit,
            CASE movement_type
                WHEN 'STOCK_IN'  THEN 'IN_WAREHOUSE'
                WHEN 'STOCK_OUT' THEN 'OUT_OF_WAREHOUSE'
            END,
            event_id, movement_type, timestamp_utc, indus_part, pallet_qty, location
        FROM (
            SELECT DISTINCT ON (handling_unit)
                handling_unit, movement_type, event_id,
                timestamp_utc, indus_part, pallet_qty, location
            FROM  movements
            WHERE handling_unit IS NOT NULL
            ORDER BY handling_unit, timestamp_utc DESC, id DESC
        ) latest
        ON CONFLICT (handling_unit) DO UPDATE SET
            current_status     = EXCLUDED.current_status,
            last_event_id      = EXCLUDED.last_event_id,
            last_movement_type = EXCLUDED.last_movement_type,
            last_movement_time = EXCLUDED.last_movement_time,
            indus_part         = EXCLUDED.indus_part,
            pallet_qty         = EXCLUDED.pallet_qty,
            last_location      = EXCLUDED.last_location
    """)
    cur.execute('SELECT COUNT(*) FROM pallet_status')
    count = cur.fetchone()[0]
    log.info(f'pallet_status populated: {count:,} unique HUs.')
    return count
 
 
def migrate_ivt(headers, rows, cur):
    if not headers:
        log.info('IVT_Line empty — nothing to migrate.')
        return 0
 
    COL = {h: i for i, h in enumerate(headers)}
    records = []
    skipped = 0
 
    for i, row in enumerate(rows, start=2):
        code_idx = COL.get('Code', 0)
        code = _clean_str(row[code_idx] if code_idx < len(row) else None)
        if not code:
            skipped += 1
            continue
 
        date_idx = COL.get('Scanned Date', COL.get('Scanned_Date', 1))
        scanned_date = _parse_timestamp(row[date_idx] if date_idx < len(row) else None)
        if scanned_date is None:
            scanned_date = datetime.now(timezone.utc)
 
        loc_idx = COL.get('Location', 2)
        location = _clean_str(row[loc_idx] if loc_idx < len(row) else None)
        records.append((code, scanned_date, location))
 
    psycopg2.extras.execute_values(
        cur,
        'INSERT INTO ivt_scans (code, scanned_date, location) VALUES %s ON CONFLICT (code) DO NOTHING',
        records,
        page_size=500
    )
    log.info(f'Inserted {len(records):,} rows into ivt_scans ({skipped} skipped).')
    return len(records)
 
 
def main():
    print("Hello World")
    parser = argparse.ArgumentParser(description='Migrate Excel workbook to Postgres.')
    parser.add_argument('--force', action='store_true',
                        help='Truncate movements + pallet_status then re-migrate from scratch.')
    args = parser.parse_args()
 
    pg = get_pg_conn()
    cur = pg.cursor()
 
    cur.execute('SELECT COUNT(*) FROM movements')
    existing = cur.fetchone()[0]
 
    if existing > 0 and not args.force:
        log.error(
            f'movements table already has {existing:,} rows. '
            'Use --force to truncate and re-run.'
        )
        cur.close(); pg.close(); sys.exit(1)
 
    if args.force and existing > 0:
        log.warning(f'--force: truncating {existing:,} rows from movements + pallet_status...')
        cur.execute('TRUNCATE TABLE pallet_status RESTART IDENTITY CASCADE;')
        cur.execute('TRUNCATE TABLE movements RESTART IDENTITY CASCADE;')
        pg.commit()
        log.info('Truncate complete.')
    else:
        # No truncation needed but we must commit the SELECT to close
        # the implicit transaction before the DDL step below.
        pg.commit()

    cur.close()
    pg.close()

    # Drop the UNIQUE constraint (+ dependent FK) using a dedicated
    # autocommit connection so there is no open transaction to interfere.
    _drop_event_id_unique_constraint(pg)
 
    # Re-open main connection for data migration
    pg = get_pg_conn()

    log.info('Connecting to SharePoint workbook...')
    sid, session_headers = _open_session()
    try:
        ml_headers, ml_rows = _read_sheet(SHEET_MOVEMENT_LOG, session_headers)
        ivt_headers, ivt_rows = _read_sheet(SHEET_IVT_LINE, session_headers)
    finally:
        _close_session(sid)
 
    if not ml_rows:
        log.error(f"'{SHEET_MOVEMENT_LOG}' returned no data rows.")
        pg.close(); sys.exit(1)
 
    log.info('Beginning Postgres transaction...')
    try:
        cur = pg.cursor()
        mv_count, ts_skipped = migrate_movements(ml_headers, ml_rows, cur)
        ps_count = derive_pallet_status(cur)
        ivt_count = migrate_ivt(ivt_headers, ivt_rows, cur)
        pg.commit()
        log.info('Transaction committed successfully.')
    except Exception as e:
        pg.rollback()
        log.error(f'Transaction rolled back: {e}')
        raise
    finally:
        cur.close(); pg.close()
 
    print()
    print('=' * 55)
    print('  MIGRATION COMPLETE')
    print('=' * 55)
    print(f'  movements rows inserted   : {mv_count:>8,}')
    print(f'  pallet_status rows        : {ps_count:>8,}  (unique HUs)')
    print(f'  ivt_scans rows inserted   : {ivt_count:>8,}')
    print('=' * 55)
    print()
 
 
if __name__ == '__main__':
    main()
