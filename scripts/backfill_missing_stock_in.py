"""
scripts/backfill_missing_stock_in.py

Purpose
-------
Repairs STOCK_OUT rows that were created via supervisor override on HUs
that had no prior STOCK_IN.  Each such row leaves a dangling debit in
the `movements` table with no matching credit, producing permanent
negative stock in the Stock by Location dashboard pivot.

Fix applied
-----------
For every affected HU a synthetic STOCK_IN row is backfilled into
`movements`, timestamped exactly 1 second BEFORE the rogue STOCK_OUT.
This creates a balanced IN/OUT pair: the debit is now matched by a
credit at the same location, which zeroes out the phantom negative stock.

The `pallet_status` row for each affected HU is then re-derived from
the most recent non-ERROR movement so it accurately reflects reality.

Safety contract
---------------
  - ZERO existing rows are modified or deleted.
  - Only INSERT (to movements) and UPDATE (to pallet_status) are used.
  - The script runs part-by-part.  You choose which Indus Part to fix,
    review the full dry-run preview, and explicitly type YES to confirm
    before any write happens.
  - Every part is wrapped in its own transaction.  Any error triggers a
    full rollback for that part so the DB is never left in a partial state.
  - The script is safe to re-run: already-fixed HUs are automatically
    excluded from the affected list on subsequent runs.
  - Backfilled rows are stamped with operator_id='BACKFILL_SCRIPT' and
    validation_status='BACKFILLED' so they are distinguishable in the
    admin audit log and can be filtered / reported on separately.

Usage
-----
    heroku run python scripts/backfill_missing_stock_in.py --app indus-warehouse-tracker
    OR locally with DATABASE_URL set in your .env

Requirements
------------
    psycopg2  (already in requirements.txt)
    DATABASE_URL environment variable
"""

import os
import sys
import uuid
from datetime import datetime, timezone, timedelta

import psycopg2
import psycopg2.extras


# ═══════════════════════════════════════════════════════════════════════════════
# DB connection
# ═══════════════════════════════════════════════════════════════════════════════

def get_connection():
    uri = os.environ.get("DATABASE_URL", "")
    if not uri:
        print("ERROR: DATABASE_URL environment variable is not set.")
        sys.exit(1)
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    try:
        conn = psycopg2.connect(uri, cursor_factory=psycopg2.extras.RealDictCursor)
        # Use autocommit=True so read queries never open an implicit transaction.
        # process_part() manages its own write transactions explicitly via
        # BEGIN / COMMIT / ROLLBACK SQL commands.
        conn.autocommit = True
        return conn
    except psycopg2.Error as exc:
        print(f"ERROR: Could not connect to Postgres: {exc}")
        sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════════
# SQL
# ═══════════════════════════════════════════════════════════════════════════════

# All distinct Indus Parts that have at least one affected HU.
# An HU is "affected" when:
#   - It has at least one STOCK_OUT with validation_status = 'OVERRIDE'
#   - AND it has zero STOCK_IN rows with a timestamp BEFORE that STOCK_OUT
FIND_AFFECTED_PARTS = """
    SELECT DISTINCT m_out.indus_part
    FROM   movements m_out
    WHERE  m_out.movement_type       = 'STOCK_OUT'
      AND  m_out.validation_status   = 'OVERRIDE'
      AND  NOT EXISTS (
               SELECT 1
               FROM   movements m_in
               WHERE  m_in.handling_unit  = m_out.handling_unit
                 AND  m_in.movement_type  = 'STOCK_IN'
                 AND  m_in.timestamp_utc  < m_out.timestamp_utc
           )
    ORDER  BY m_out.indus_part;
"""

# All affected HUs for a specific Indus Part.
# DISTINCT ON (handling_unit) returns the earliest rogue STOCK_OUT per HU.
FIND_AFFECTED_HUS_FOR_PART = """
    SELECT DISTINCT ON (m_out.handling_unit)
           m_out.handling_unit,
           m_out.indus_part,
           m_out.customer_part,
           m_out.pallet_qty,
           m_out.location,
           m_out.job_no,
           m_out.timestamp_utc,
           m_out.device_id
    FROM   movements m_out
    WHERE  m_out.movement_type      = 'STOCK_OUT'
      AND  m_out.validation_status  = 'OVERRIDE'
      AND  m_out.indus_part         = %(indus_part)s
      AND  NOT EXISTS (
               SELECT 1
               FROM   movements m_in
               WHERE  m_in.handling_unit  = m_out.handling_unit
                 AND  m_in.movement_type  = 'STOCK_IN'
                 AND  m_in.timestamp_utc  < m_out.timestamp_utc
           )
    ORDER  BY m_out.handling_unit,
              m_out.timestamp_utc ASC;
"""

# Idempotency guard: check if a BACKFILLED STOCK_IN already exists for this HU.
CHECK_ALREADY_BACKFILLED = """
    SELECT 1
    FROM   movements
    WHERE  handling_unit      = %(hu)s
      AND  movement_type      = 'STOCK_IN'
      AND  validation_status  = 'BACKFILLED'
    LIMIT  1;
"""

# Insert the backfilled STOCK_IN row.
INSERT_BACKFILL_STOCK_IN = """
    INSERT INTO movements (
        event_id,
        timestamp_utc,
        handling_unit,
        indus_part,
        customer_part,
        pallet_qty,
        job_no,
        movement_type,
        operator_id,
        device_id,
        sync_status,
        validation_status,
        location,
        from_location,
        to_location
    ) VALUES (
        %(event_id)s,
        %(timestamp_utc)s,
        %(handling_unit)s,
        %(indus_part)s,
        %(customer_part)s,
        %(pallet_qty)s,
        %(job_no)s,
        'STOCK_IN',
        'BACKFILL_SCRIPT',
        %(device_id)s,
        'SYNCED',
        'BACKFILLED',
        %(location)s,
        '',
        ''
    );
"""

# Re-derive pallet_status from the most recent non-ERROR movement for this HU.
GET_LATEST_MOVEMENT_FOR_HU = """
    SELECT event_id, movement_type, timestamp_utc,
           indus_part, pallet_qty, location
    FROM   movements
    WHERE  handling_unit  = %(hu)s
      AND  movement_type <> 'ERROR'
    ORDER  BY timestamp_utc DESC, id DESC
    LIMIT  1;
"""

UPSERT_PALLET_STATUS = """
    INSERT INTO pallet_status (
        handling_unit,
        current_status,
        last_event_id,
        last_movement_type,
        last_movement_time,
        indus_part,
        pallet_qty,
        last_location
    ) VALUES (
        %(handling_unit)s,
        %(current_status)s,
        %(last_event_id)s,
        %(last_movement_type)s,
        %(last_movement_time)s,
        %(indus_part)s,
        %(pallet_qty)s,
        %(last_location)s
    )
    ON CONFLICT (handling_unit) DO UPDATE SET
        current_status     = EXCLUDED.current_status,
        last_event_id      = EXCLUDED.last_event_id,
        last_movement_type = EXCLUDED.last_movement_type,
        last_movement_time = EXCLUDED.last_movement_time,
        indus_part         = EXCLUDED.indus_part,
        pallet_qty         = EXCLUDED.pallet_qty,
        last_location      = EXCLUDED.last_location;
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _hr(char='─', width=68):
    print(char * width)


def _banner(title):
    _hr('═')
    print(f'  {title}')
    _hr('═')


def _ts_minus_one_second(ts) -> str:
    """
    Subtract 1 second from a timestamp value.
    Accepts a psycopg2 datetime object or an ISO-string.
    Returns an ISO-format UTC string.
    """
    if isinstance(ts, str):
        ts_clean = ts.replace('Z', '+00:00')
        try:
            dt = datetime.fromisoformat(ts_clean)
        except ValueError:
            dt = datetime.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S')
            dt = dt.replace(tzinfo=timezone.utc)
    else:
        # psycopg2 returns aware datetime objects for TIMESTAMPTZ columns
        dt = ts

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return (dt - timedelta(seconds=1)).isoformat()


def _make_event_id() -> str:
    """Generate a script-scoped event_id (BKFL- prefix) for easy audit filtering."""
    return 'BKFL-' + str(uuid.uuid4()).upper()[:12]


def _prompt_yes_no(question: str) -> bool:
    """Requires explicit 'YES' to proceed. Anything else returns False."""
    answer = input(f'\n  {question} [type YES to confirm, anything else to skip]: ').strip()
    if answer.upper() == 'YES':
        return True
    print('  Skipped.')
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# Core logic
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_affected_parts(conn) -> list:
    """Return sorted list of distinct indus_part values that have affected HUs."""
    with conn.cursor() as cur:
        cur.execute(FIND_AFFECTED_PARTS)
        rows = cur.fetchall()
    return [r['indus_part'] for r in rows]


def fetch_affected_hus(conn, indus_part: str) -> list:
    """Return all affected HU rows for a given indus_part."""
    with conn.cursor() as cur:
        cur.execute(FIND_AFFECTED_HUS_FOR_PART, {'indus_part': indus_part})
        return cur.fetchall()


def is_already_backfilled(cur, hu: str) -> bool:
    """Return True if a BACKFILLED STOCK_IN already exists for this HU."""
    cur.execute(CHECK_ALREADY_BACKFILLED, {'hu': hu})
    return cur.fetchone() is not None


def process_part(conn, indus_part: str, affected_hus: list) -> dict:
    """
    Backfill STOCK_IN for all affected HUs of one Indus Part.

    The connection must be in autocommit=True mode (set by get_connection).
    This function issues explicit BEGIN / COMMIT / ROLLBACK SQL so that
    all HUs for the part are committed atomically, with a clean rollback
    on any error.  This avoids psycopg2.ProgrammingError: set_session
    cannot be used inside a transaction.

    Returns a summary dict:
        inserted      int   — rows inserted into movements
        skipped       int   — HUs already backfilled (idempotency)
        ps_updated    int   — pallet_status rows reconciled
        rolled_back   bool  — True if the transaction was rolled back
    """
    summary = {'inserted': 0, 'skipped': 0, 'ps_updated': 0, 'rolled_back': False}

    cur = conn.cursor()
    try:
        cur.execute('BEGIN')

        for row in affected_hus:
            hu         = row['handling_unit']
            indus_p    = row['indus_part'] or ''
            customer_p = row['customer_part'] or ''
            pallet_qty = row['pallet_qty'] or ''
            location   = row['location'] or ''
            job_no     = row['job_no'] or ''
            rogue_ts   = row['timestamp_utc']
            device_id  = row['device_id'] or ''

            # ── Idempotency guard ─────────────────────────────────
            if is_already_backfilled(cur, hu):
                print(f'  ⚠  {hu} — already backfilled, skipping.')
                summary['skipped'] += 1
                continue

            # ── Build and insert the backfill STOCK_IN row ──────────────
            backfill_ts = _ts_minus_one_second(rogue_ts)
            event_id    = _make_event_id()

            cur.execute(INSERT_BACKFILL_STOCK_IN, {
                'event_id':      event_id,
                'timestamp_utc': backfill_ts,
                'handling_unit': hu,
                'indus_part':    indus_p,
                'customer_part': customer_p,
                'pallet_qty':    pallet_qty,
                'job_no':        job_no,
                'location':      location,
                'device_id':     device_id,
            })
            summary['inserted'] += 1
            print(f'  ✓  STOCK_IN backfilled : {hu}  '
                  f'part={indus_p}  qty={pallet_qty}  '
                  f'loc={location}  ts={backfill_ts}  event={event_id}')

            # ── Reconcile pallet_status ───────────────────────────
            # Re-derive from most recent non-ERROR movement (includes the
            # row we just inserted, since we're inside the same transaction).
            cur.execute(GET_LATEST_MOVEMENT_FOR_HU, {'hu': hu})
            latest = cur.fetchone()

            if latest:
                mv_type = (latest['movement_type'] or '').upper()
                current_status = (
                    'IN_WAREHOUSE'
                    if mv_type == 'STOCK_IN'
                    else 'OUT_OF_WAREHOUSE'
                )
                cur.execute(UPSERT_PALLET_STATUS, {
                    'handling_unit':      hu,
                    'current_status':     current_status,
                    'last_event_id':      latest['event_id'],
                    'last_movement_type': latest['movement_type'],
                    'last_movement_time': str(latest['timestamp_utc']),
                    'indus_part':         latest['indus_part'] or '',
                    'pallet_qty':         latest['pallet_qty'] or '',
                    'last_location':      latest['location'] or '',
                })
                summary['ps_updated'] += 1
                print(f'        pallet_status → status={current_status}  '
                      f'loc={latest["location"]}')
            else:
                print(f'        pallet_status: no non-ERROR movement found, left as-is.')

        cur.execute('COMMIT')
        print(f'\n  ✓  Transaction committed for part: {indus_part}')

    except Exception as exc:
        cur.execute('ROLLBACK')
        summary['rolled_back'] = True
        print(f'\n  ✗  ERROR — full rollback applied for part: {indus_part}')
        print(f'     Exception: {exc}')
        print(f'     Database unchanged for this part.')
    finally:
        cur.close()

    return summary


# ═══════════════════════════════════════════════════════════════════════════════
# Main interactive flow
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    _banner('backfill_missing_stock_in.py')
    print()
    print('  PURPOSE:')
    print('  Backfills a synthetic STOCK_IN for every HU that had a')
    print('  STOCK_OUT via supervisor override but NO prior STOCK_IN.')
    print('  This eliminates phantom negative stock in Stock by Location.')
    print()
    print('  SAFETY:')
    print('  ✓  ZERO existing rows are modified or deleted.')
    print('  ✓  Runs part-by-part with explicit YES confirmation each time.')
    print('  ✓  Each part is a separate transaction — error = full rollback.')
    print('  ✓  Already-fixed HUs are automatically skipped (safe to re-run).')
    print('  ✓  Backfilled rows are tagged BACKFILLED for audit trail.')
    _hr()

    conn = get_connection()
    print('  ✓  Connected to database.\n')

    # ── Step 1: Find all affected parts ─────────────────────────────────────
    print('[SCAN] Scanning database for affected Indus Parts...')
    affected_parts = fetch_affected_parts(conn)

    if not affected_parts:
        print()
        print('  ✓  No affected HUs found. Nothing to fix.')
        print('     Either all cases were already backfilled, or there were')
        print('     no rogue STOCK_OUT overrides with no prior STOCK_IN.')
        conn.close()
        sys.exit(0)

    print(f'  Found {len(affected_parts)} Indus Part(s) with affected HUs:\n')
    for i, part in enumerate(affected_parts, 1):
        print(f'    [{i}]  {part}')
    print(f'    [A]  Process ALL parts in sequence')
    print(f'    [Q]  Quit without making any changes')
    print()

    # ── Step 2: Interactive menu loop ─────────────────────────────────────────
    grand_total = {'inserted': 0, 'skipped': 0, 'ps_updated': 0, 'errors': 0}

    while True:
        choice = input('  Enter part number, A for all, or Q to quit: ').strip().upper()

        if choice == 'Q':
            print()
            print('  Quit requested. No changes made.')
            conn.close()
            sys.exit(0)

        parts_to_process = []

        if choice == 'A':
            parts_to_process = list(affected_parts)
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(affected_parts):
                parts_to_process = [affected_parts[idx]]
            else:
                print(f'  Invalid choice. Enter 1–{len(affected_parts)}, A, or Q.')
                continue
        else:
            print('  Invalid input. Enter a number, A, or Q.')
            continue

        # ── Step 3: Dry-run preview ───────────────────────────────────────────
        print()
        _hr()
        print('  DRY-RUN PREVIEW — the following inserts will be made:')
        _hr()

        all_rows        = []   # list of (indus_part, [hu_rows])
        total_to_insert = 0
        total_to_skip   = 0

        for part in parts_to_process:
            hus = fetch_affected_hus(conn, part)
            if not hus:
                continue

            with conn.cursor() as cur:
                new_hus     = []
                skipped_hus = []
                for h in hus:
                    if is_already_backfilled(cur, h['handling_unit']):
                        skipped_hus.append(h)
                    else:
                        new_hus.append(h)

            if new_hus:
                all_rows.append((part, new_hus))
                total_to_insert += len(new_hus)
                print(f'\n  Part: {part}  ({len(new_hus)} HU(s) to backfill)')
                print(f'  {"-" * 64}')
                print(f'  {"HU":<22} {"QTY":<8} {"LOC":<12} ROGUE OUT TIMESTAMP')
                print(f'  {"-" * 22} {"-" * 8} {"-" * 12} {"-" * 25}')
                for h in new_hus:
                    ts_str = str(h['timestamp_utc'])[:19] if h['timestamp_utc'] else 'N/A'
                    print(
                        f'  {str(h["handling_unit"] or ""):<22} '
                        f'{str(h["pallet_qty"] or ""):<8} '
                        f'{str(h["location"] or ""):<12} '
                        f'{ts_str}'
                    )

            if skipped_hus:
                total_to_skip += len(skipped_hus)
                print(f'  ⚠  {len(skipped_hus)} HU(s) already backfilled (will be skipped):')
                for h in skipped_hus:
                    print(f'       {h["handling_unit"]}')

        if total_to_insert == 0:
            print()
            print('  ✓  All HUs for the selected part(s) are already backfilled.')
            print('     Nothing to do. Returning to menu...\n')
            continue

        print()
        _hr()
        print('  SUMMARY OF PENDING CHANGES:')
        print(f'    STOCK_IN rows to INSERT  : {total_to_insert}')
        print(f'    pallet_status to UPDATE  : {total_to_insert}')
        print(f'    Already backfilled (skip): {total_to_skip}')
        print(f'    Existing rows modified   : 0  ← guaranteed')
        print(f'    Existing rows deleted    : 0  ← guaranteed')
        _hr()

        if not _prompt_yes_no(
            f'Proceed with backfilling {total_to_insert} STOCK_IN row(s)?'
        ):
            print('  No changes made. Returning to menu.\n')
            continue

        # ── Step 4: Execute ───────────────────────────────────────────────────
        for part, hu_rows in all_rows:
            print()
            _hr()
            print(f'  Processing part: {part}  ({len(hu_rows)} HU(s))')
            _hr()

            result = process_part(conn, part, hu_rows)

            grand_total['inserted']   += result['inserted']
            grand_total['skipped']    += result['skipped']
            grand_total['ps_updated'] += result['ps_updated']
            if result['rolled_back']:
                grand_total['errors'] += 1

        # ── Step 5: Final summary ───────────────────────────────────────────────
        print()
        _banner('COMPLETE')
        print(f'  STOCK_IN rows inserted   : {grand_total["inserted"]}')
        print(f'  pallet_status updated    : {grand_total["ps_updated"]}')
        print(f'  HUs skipped (idempotent) : {grand_total["skipped"]}')
        print(f'  Parts with errors        : {grand_total["errors"]}')
        print()
        if grand_total['errors'] == 0:
            print('  ✓  All changes committed successfully.')
        else:
            print('  ⚠  Some parts had errors and were rolled back.')
            print('     Re-run the script to retry those parts.')
        print()
        print("  AUDIT: SELECT * FROM movements WHERE validation_status = 'BACKFILLED'")
        print('         to see all rows inserted by this script.')
        _hr('═')
        break

    conn.close()


if __name__ == "__main__":
    run()
