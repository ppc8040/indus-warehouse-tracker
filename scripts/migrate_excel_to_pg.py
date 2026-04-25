"""
scripts/migrate_excel_to_pg.py

One-time migration script: reads Movement_Log and IVT_Line from the live
SharePoint Excel workbook via Microsoft Graph API and bulk-inserts all rows
into the Postgres tables created by create_schema.py.

After inserting all movements, derives pallet_status for every HU from
its latest movement record.

Run ONCE after create_schema.py has been executed successfully.

Usage:
    heroku run --app indus-warehouse-tracker python scripts/migrate_excel_to_pg.py
    OR locally with both DATABASE_URL and Graph credentials set in your .env

Re-run (clean slate):
    heroku run --app indus-warehouse-tracker python scripts/migrate_excel_to_pg.py --force
    (--force truncates movements + pallet_status before re-inserting)

Safety:
    - Checks for existing rows in `movements` before inserting.
    - Will abort if movements table is non-empty to prevent double-migration.
    - Use --force flag to bypass this check and get a clean re-run.
    - All inserts are wrapped in a single transaction; rolls back fully on error.
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


# ── Config (─────────────────────────────────────────────────────

GRAPH_TENANT_ID      = 'ed97e9bb-e119-4bd4-ab00-307d64bdf908'
GRAPH_CLIENT_ID      = 'a2239be7-12cd-442d-983f-ea7e316ef767'
GRAPH_CLIENT_SECRET  = 'sho8Q~sanlYEX3SKXjYJb0NSXL.gPRpdi63-tcmp'
SHAREPOINT_SITE_ID   = '201c767f-23cc-4eff-b76c-47a275706531,094b21f9-6ed4-47c3-a751-3b217a18b98e'
WORKBOOK_FILENAME    = 'Indus Warehouse Stock Movement.xlsx'
SHEET_MOVEMENT_LOG   = 'Movement_Log'
SHEET_IVT_LINE       = 'IVT_Line'


# ── Postgres connection ───────────────────────────────────────────────────

def get_pg_conn():
    uri = os.environ.get('DATABASE_URL')
    if not uri:
        log.error('DATABASE_URL is not set. Aborting.')
        sys.exit(1)
    if uri.startswith('postgres://'):
        uri = uri.replace('postgres://', 'postgresql://', 1)
    return psycopg2.connect(uri)


# ── Graph API helpers ──────────────────────────────────────────────────

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


# ── Value helpers ─────────────────────────────────────────────────────────

def _clean_str(val):
    if val is None:
        return None
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


def _normalise_ts_string(raw):
    """
    Clean a raw cell value into a consistent string before date parsing.
    Handles every real-world messiness Graph API produces:
      - Double/triple spaces            '12/4/2026  4:02:28 PM'
      - Non-breaking spaces (\u00a0)
      - Zero-width spaces / BOM chars
      - Leading/trailing whitespace
      - Apostrophe prefix from _safe_row()
    Returns a clean single-spaced ASCII-safe string.
    """
    s = str(raw)
    # Strip invisible Unicode: NBSP, zero-width space, BOM, soft hyphen
    s = s.replace('\u00a0', ' ')   # non-breaking space
    s = s.replace('\u200b', '')    # zero-width space
    s = s.replace('\ufeff', '')    # BOM
    s = s.replace('\u00ad', '')    # soft hyphen
    # Strip apostrophe prefix
    s = s.lstrip("'")
    # Collapse all whitespace runs to a single space
    s = ' '.join(s.split())
    return s.strip()


# All strptime patterns tried in order after dateutil.
# Covers: D/M/YYYY and DD/MM/YYYY, 12h and 24h, with/without seconds,
# Excel export format, ISO 8601 variants.
_TS_FORMATS = [
    # DD/MM/YYYY — app's canonical format
    '%d/%m/%Y %I:%M:%S %p',    # 25/04/2026 12:30:04 PM
    '%d/%m/%Y %H:%M:%S',       # 25/04/2026 12:30:04
    '%d/%m/%Y %I:%M %p',       # 25/04/2026 12:30 PM
    '%d/%m/%Y %H:%M',          # 25/04/2026 12:30
    '%d/%m/%Y',                # 25/04/2026
    # D/M/YYYY — no zero-padding (common from older rows)
    '%d/%m/%Y %I:%M:%S %p',
    '%d/%m/%Y %H:%M:%S',
    # M/D/YYYY — American format guard (shouldn’t occur but covered)
    '%m/%d/%Y %I:%M:%S %p',
    '%m/%d/%Y %H:%M:%S',
    '%m/%d/%Y %H:%M',
    # ISO 8601 variants
    '%Y-%m-%dT%H:%M:%S',
    '%Y-%m-%dT%H:%M:%SZ',
    '%Y-%m-%dT%H:%M:%S.%f',
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%d %H:%M',
    '%Y-%m-%d',
    # Excel export variants
    '%d-%m-%Y %H:%M:%S',
    '%d-%m-%Y %H:%M',
    '%d %b %Y %H:%M:%S',       # 25 Apr 2026 12:30:04
    '%d %B %Y %H:%M:%S',       # 25 April 2026 12:30:04
    '%b %d, %Y %I:%M:%S %p',   # Apr 25, 2026 12:30:04 PM
]

# Regex to extract date+time components as a last resort from garbled strings.
# Matches patterns like: 12/4/2026 4:02:28 PM  or  2026-04-12 16:02
_TS_REGEX = re.compile(
    r'(\d{1,4})[/\-\.](\d{1,2})[/\-\.](\d{2,4})'
    r'(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*(AM|PM))?)?',
    re.IGNORECASE
)


def _regex_fallback(s):
    """
    Last-resort: extract numeric date+time components via regex and
    reconstruct a datetime. Only used when dateutil and strptime both fail.
    Assumes DD/MM/YYYY ordering (matching the app's canonical format).
    """
    m = _TS_REGEX.search(s)
    if not m:
        return None
    p1, p2, p3, hh, mm, ss, ampm = m.groups()
    p1, p2, p3 = int(p1), int(p2), int(p3)

    # Determine year, month, day
    if p3 > 31:          # p3 is 4-digit year → YYYY/MM/DD or YYYY-MM-DD
        year, month, day = p3, p2, p1
    elif p1 > 31:        # p1 is 4-digit year
        year, month, day = p1, p2, p3
    else:                # assume DD/MM/YYYY
        day, month, year = p1, p2, p3
        if year < 100:   # 2-digit year
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
        # Swap day/month and retry (month > 12 means we guessed wrong)
        try:
            return datetime(year, day, month, hour, minute, second)
        except ValueError:
            return None


def _parse_timestamp(val):
    """
    Parse any timestamp value from Excel into a UTC-aware datetime.

    Pipeline:
      1.  Normalise string (collapse spaces, strip invisible chars)
      2.  Excel numeric serial date
      3.  dateutil.parser.parse() with dayfirst=True  ← handles ~99% of cases
      4.  strptime loop over _TS_FORMATS
      5.  Regex extraction fallback (_regex_fallback)
      6.  Return None only if all five stages fail

    All Dubai-local datetimes are shifted to UTC by subtracting 4 hours.
    """
    if val is None:
        return None

    s = _normalise_ts_string(val)
    if not s:
        return None

    # Stage 1: Excel numeric serial date
    try:
        serial = float(s)
        if serial > 1:
            base = datetime(1899, 12, 30, tzinfo=timezone.utc)
            return base + timedelta(days=serial)
    except ValueError:
        pass

    def _to_utc(dt_naive):
        """Treat naive datetime as Dubai local (+04) and convert to UTC."""
        return dt_naive.replace(tzinfo=None).replace(tzinfo=timezone.utc) - timedelta(hours=4)

    # Stage 2: dateutil — format-agnostic, handles virtually everything
    if _DATEUTIL_AVAILABLE:
        try:
            return _to_utc(dateutil_parser.parse(s, dayfirst=True))
        except (ValueError, OverflowError, TypeError):
            pass

    # Stage 3: strptime loop
    for fmt in _TS_FORMATS:
        try:
            return _to_utc(datetime.strptime(s, fmt))
        except ValueError:
            continue

    # Stage 4: regex last resort
    dt = _regex_fallback(s)
    if dt is not None:
        return _to_utc(dt)

    return None   # genuinely unparseable


def _print_ts_diagnostic(rows, ts_col_idx, label='Movement_Log', n=10):
    print()
    print(f'  ── Timestamp diagnostic ({label}, first {n} non-empty values) ──')
    shown = 0
    for row in rows:
        if ts_col_idx < len(row) and row[ts_col_idx]:
            raw = row[ts_col_idx]
            parsed = _parse_timestamp(raw)
            status = f'→ {parsed.isoformat()}' if parsed else '→ UNPARSEABLE ⚠'
            print(f'    raw: {repr(str(raw)):<45}  {status}')
            shown += 1
            if shown >= n:
                break
    print()


# ── Migration: Movement_Log → movements + pallet_status ──────────────────

def migrate_movements(headers, rows, cur):
    COL = {h: i for i, h in enumerate(headers)}

    required = ['Event_ID', 'Timestamp_UTC', 'Handling_Unit', 'Movement_Type']
    missing = [c for c in required if c not in COL]
    if missing:
        log.error(f'Movement_Log is missing required columns: {missing}')
        log.error(f'Columns found: {headers}')
        sys.exit(1)

    ts_idx = COL.get('Timestamp_UTC', 1)
    _print_ts_diagnostic(rows, ts_idx, label='Movement_Log')

    def g(row, col, default=None):
        idx = COL.get(col)
        if idx is None or idx >= len(row):
            return default
        return row[idx]

    records = []
    skipped_no_id = 0
    skipped_bad_type = 0
    skipped_bad_ts = []

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
        if timestamp is None:
            skipped_bad_ts.append((i, event_id, repr(raw_ts)))
            continue

        sync_status = _clean_str(g(row, 'Sync_Status')) or 'SYNCED'
        if sync_status not in ('SYNCED', 'PENDING'):
            sync_status = 'SYNCED'

        validation_status = _clean_str(g(row, 'Validation_Status')) or 'VALID'
        if validation_status not in ('VALID', 'OVERRIDE'):
            validation_status = 'VALID'

        records.append((
            event_id,
            timestamp,
            _clean_str(g(row, 'Handling_Unit')),
            _clean_str(g(row, 'Indus_Part')),
            _clean_str(g(row, 'Customer_Part')),
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
        f'{len(skipped_bad_ts)} still-unparseable skipped).'
    )

    if skipped_bad_ts:
        print()
        print(f'  ⚠  {len(skipped_bad_ts)} rows still skipped (genuinely unparseable timestamp):')
        for row_n, eid, raw in skipped_bad_ts[:20]:
            print(f'     row {row_n:>5}  event_id={eid}  raw_ts={raw}')
        if len(skipped_bad_ts) > 20:
            print(f'     ... and {len(skipped_bad_ts) - 20} more')
        print()

    if not records:
        log.error('No valid records to insert. Aborting.')
        sys.exit(1)

    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO movements
            (event_id, timestamp_utc, handling_unit, indus_part, customer_part,
             pallet_qty, job_no, movement_type, operator_id, device_id,
             sync_status, validation_status, location, destination)
        VALUES %s
        ON CONFLICT (event_id) DO NOTHING
        """,
        records,
        page_size=500
    )
    log.info(f'Inserted {len(records):,} rows into movements.')
    return len(records), len(skipped_bad_ts)


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


# ── Migration: IVT_Line → ivt_scans ───────────────────────────────────

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


# ── Main ───────────────────────────────────────────────────────────────────

def main():
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

    cur.close()

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
    print(f'  rows skipped (bad ts)     : {ts_skipped:>8,}')
    print(f'  pallet_status rows        : {ps_count:>8,}  (unique HUs)')
    print(f'  ivt_scans rows inserted   : {ivt_count:>8,}')
    print('=' * 55)
    if ts_skipped > 0:
        print()
        print('  ⚠  Check the skipped rows above — these are genuinely')
        print('     unreadable timestamps that no parser could recover.')
    print()


if __name__ == '__main__':
    main()
