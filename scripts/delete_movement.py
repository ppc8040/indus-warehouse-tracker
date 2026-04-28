"""
scripts/delete_movement.py

Purpose
-------
Interactive CLI tool to safely delete a single row from the `movements`
table and immediately recalculate `pallet_status` for the affected HU.

Use case
--------
A rogue or duplicate movement row (e.g. an accidental STOCK_OUT, a test
scan, a mis-entered record) needs to be permanently removed from the DB.
Deleting the row via raw SQL would leave `pallet_status` stale.  This
script handles both in one atomic transaction.

What this script does per deletion
-----------------------------------
  1. Accepts a numeric `id` (the movements PK) as input.
  2. Fetches and displays ALL columns of that row so you can verify it
     is the correct row before anything is touched.
  3. Asks for explicit [yes/no] confirmation.
  4. Inside a single transaction:
       a. DELETE the row from `movements`.
       b. Re-derive the latest movement for that HU using the same
          DISTINCT ON logic used by the admin dashboard endpoints.
       c. UPSERT `pallet_status` for that HU with the recalculated
          values.  If the deleted row was the ONLY row for this HU,
          the HU is left in `pallet_status` as UNKNOWN (no data).
  5. Prints before/after counts and the new pallet_status for the HU.
  6. Loops — asks whether to delete another row.

Safety contract
---------------
  - The deletion and pallet_status update are wrapped in ONE transaction.
    Any error → full ROLLBACK; movements row is NOT deleted.
  - The full row is shown and requires explicit "yes" before writing.
  - Nothing is written on "no" or any unexpected input.
  - No other tables (idempotency_keys, ivt_scans, etc.) are touched.
  - ID gaps left by deletion are normal PostgreSQL behaviour — sequences
    are never renumbered (doing so would break idempotency keys / event_id
    lookups and is unnecessary).

Usage
-----
    heroku run python scripts/delete_movement.py --app indus-warehouse-tracker
    OR locally with DATABASE_URL exported in your shell.
"""

import os
import sys
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras


# ============================================================
# DB CONNECTION
# ============================================================

def get_connection():
    uri = os.environ.get('DATABASE_URL', '').strip()
    if not uri:
        _die('DATABASE_URL environment variable is not set.')
    # Heroku still uses the legacy postgres:// scheme; psycopg2 needs postgresql://
    if uri.startswith('postgres://'):
        uri = uri.replace('postgres://', 'postgresql://', 1)
    try:
        conn = psycopg2.connect(uri, cursor_factory=psycopg2.extras.RealDictCursor)
        conn.autocommit = False  # all writes use explicit transactions
        return conn
    except psycopg2.Error as exc:
        _die(f'Cannot connect to Postgres: {exc}')


# ============================================================
# DISPLAY HELPERS
# ============================================================

COL_W = 76


def _hr(char='─', width=COL_W):
    print(char * width)


def _banner(text):
    print()
    _hr('═')
    print(f'  {text}')
    _hr('═')


def _die(msg):
    print(f'\n  ✗  ERROR: {msg}')
    sys.exit(1)


def _fmt(val):
    """Format a DB value for clean display."""
    if val is None:
        return '—'
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.strftime('%Y-%m-%d %H:%M:%S UTC')
    return str(val)


# Ordered list of columns to display, with friendly labels.
# id is shown first; remaining in logical groups.
_DISPLAY_COLS = [
    ('id',                'id'),
    ('event_id',          'event_id'),
    ('timestamp_utc',     'timestamp_utc'),
    ('handling_unit',     'handling_unit'),
    ('indus_part',        'indus_part'),
    ('customer_part',     'customer_part'),
    ('pallet_qty',        'pallet_qty'),
    ('job_no',            'job_no'),
    ('movement_type',     'movement_type'),
    ('operator_id',       'operator_id'),
    ('device_id',         'device_id'),
    ('location',          'location'),
    ('from_location',     'from_location'),
    ('to_location',       'to_location'),
    ('sync_status',       'sync_status'),
    ('validation_status', 'validation_status'),
]


def print_row(row: dict):
    """Pretty-print all columns of a movements row."""
    print()
    _hr()
    print(f'  {"FIELD":<22}  VALUE')
    _hr()
    for key, label in _DISPLAY_COLS:
        val = row.get(key)
        # Fall back gracefully for columns that might not exist in older DB schemas
        display_val = _fmt(val) if val is not None else '—'
        print(f'  {label:<22}  {display_val}')
    # Print any extra columns not in _DISPLAY_COLS (future-proofing)
    known = {k for k, _ in _DISPLAY_COLS}
    for key, val in row.items():
        if key not in known:
            print(f'  {key:<22}  {_fmt(val)}')
    _hr()


# ============================================================
# MOVEMENTS QUERIES
# ============================================================

FETCH_MOVEMENT_BY_ID = """
    SELECT
        id, event_id,
        timestamp_utc,
        handling_unit, indus_part, customer_part, pallet_qty,
        job_no, movement_type, operator_id, device_id,
        location, from_location, to_location,
        sync_status, validation_status
    FROM movements
    WHERE id = %(row_id)s
"""

COUNT_MOVEMENTS = "SELECT COUNT(*) AS cnt FROM movements"


# ============================================================
# PALLET STATUS RECALCULATION
# ============================================================

# Re-derive the latest non-ERROR movement for a specific HU after deletion.
# Uses the same DISTINCT ON + id DESC tiebreaker as the admin dashboard.
FETCH_LATEST_MOVEMENT_FOR_HU = """
    SELECT DISTINCT ON (handling_unit)
        handling_unit,
        event_id,
        movement_type,
        timestamp_utc,
        indus_part,
        pallet_qty,
        location
    FROM movements
    WHERE handling_unit = %(hu)s
      AND movement_type IN ('STOCK_IN', 'STOCK_OUT')
    ORDER BY handling_unit, timestamp_utc DESC, id DESC
"""

UPSERT_PALLET_STATUS = """
    INSERT INTO pallet_status (
        handling_unit, current_status, last_event_id,
        last_movement_type, last_movement_time,
        indus_part, pallet_qty, last_location
    ) VALUES (
        %(handling_unit)s, %(current_status)s, %(last_event_id)s,
        %(last_movement_type)s, %(last_movement_time)s,
        %(indus_part)s, %(pallet_qty)s, %(last_location)s
    )
    ON CONFLICT (handling_unit) DO UPDATE SET
        current_status      = EXCLUDED.current_status,
        last_event_id       = EXCLUDED.last_event_id,
        last_movement_type  = EXCLUDED.last_movement_type,
        last_movement_time  = EXCLUDED.last_movement_time,
        indus_part          = EXCLUDED.indus_part,
        pallet_qty          = EXCLUDED.pallet_qty,
        last_location       = EXCLUDED.last_location
"""

SET_PALLET_STATUS_UNKNOWN = """
    UPDATE pallet_status
    SET current_status     = 'UNKNOWN',
        last_movement_type = NULL,
        last_movement_time = NULL,
        last_event_id      = NULL
    WHERE handling_unit = %(hu)s
"""


def _status_from_movement(movement_type: str) -> str:
    mt = (movement_type or '').strip().upper()
    if mt == 'STOCK_IN':
        return 'IN_WAREHOUSE'
    if mt == 'STOCK_OUT':
        return 'OUT_OF_WAREHOUSE'
    return 'UNKNOWN'


def recalculate_pallet_status(cur, hu: str) -> dict:
    """
    Re-derive and UPSERT pallet_status for `hu` using the remaining
    movements rows (called after the deletion has already happened
    within the same open transaction).

    Returns a dict describing the new state for display.
    """
    cur.execute(FETCH_LATEST_MOVEMENT_FOR_HU, {'hu': hu})
    latest = cur.fetchone()

    if latest is None:
        # No STOCK_IN / STOCK_OUT movements remain for this HU.
        # Mark it UNKNOWN in pallet_status rather than leaving stale data.
        cur.execute(SET_PALLET_STATUS_UNKNOWN, {'hu': hu})
        return {
            'hu':     hu,
            'status': 'UNKNOWN',
            'note':   'No STOCK_IN/STOCK_OUT movements remain for this HU.',
        }

    latest = dict(latest)
    new_status = _status_from_movement(latest['movement_type'])

    params = {
        'handling_unit'     : hu,
        'current_status'    : new_status,
        'last_event_id'     : latest.get('event_id'),
        'last_movement_type': latest.get('movement_type'),
        'last_movement_time': latest.get('timestamp_utc'),
        'indus_part'        : (latest.get('indus_part') or '').strip(),
        'pallet_qty'        : str(latest.get('pallet_qty') or ''),
        'last_location'     : (latest.get('location') or '').strip().upper(),
    }
    cur.execute(UPSERT_PALLET_STATUS, params)

    return {
        'hu'      : hu,
        'status'  : new_status,
        'location': params['last_location'],
        'mv_type' : params['last_movement_type'],
        'ts'      : _fmt(latest.get('timestamp_utc')),
        'note'    : None,
    }


# ============================================================
# CORE DELETE FLOW
# ============================================================

def fetch_row(conn, row_id: int):
    """Return the movements row dict for row_id, or None if not found."""
    with conn.cursor() as cur:
        cur.execute(FETCH_MOVEMENT_BY_ID, {'row_id': row_id})
        row = cur.fetchone()
    return dict(row) if row else None


def count_movements(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(COUNT_MOVEMENTS)
        row = cur.fetchone()
    return int(row['cnt']) if row else 0


def delete_and_recalculate(conn, row_id: int, hu: str) -> dict:
    """
    Delete `row_id` from movements and recalculate pallet_status for `hu`
    in a single atomic transaction.

    Returns:
        {'ok': True,  'ps': <new pallet_status dict>}
        {'ok': False, 'error': <str>}
    """
    try:
        with conn.cursor() as cur:
            cur.execute('BEGIN')

            # 1. Delete the movement row
            cur.execute('DELETE FROM movements WHERE id = %(id)s', {'id': row_id})
            deleted = cur.rowcount
            if deleted == 0:
                cur.execute('ROLLBACK')
                return {'ok': False, 'error': f'Row {row_id} not found (may have been deleted already).'}

            # 2. Recalculate pallet_status for this HU
            ps_result = recalculate_pallet_status(cur, hu)

            cur.execute('COMMIT')
            return {'ok': True, 'ps': ps_result}

    except psycopg2.Error as exc:
        try:
            with conn.cursor() as cur:
                cur.execute('ROLLBACK')
        except Exception:
            pass
        return {'ok': False, 'error': str(exc)}


# ============================================================
# INPUT HELPERS
# ============================================================

def prompt_id() -> int | None:
    """
    Prompt for a row ID.  Returns int on valid input,
    None if user typed 'q' / 'quit' / empty line to exit.
    """
    while True:
        try:
            raw = input('\n  Enter movements row ID to inspect (or q to quit): ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if raw.lower() in ('q', 'quit', 'exit', ''):
            return None

        if raw.isdigit() and int(raw) > 0:
            return int(raw)

        print('  ⚠  Please enter a positive integer ID (e.g. 4821), or "q" to quit.')


def prompt_confirm(prompt_text: str) -> bool:
    """
    Ask a yes/no question.  Returns True only for exact "yes" (case-insensitive).
    Any other input is treated as No.
    """
    while True:
        try:
            raw = input(f'  {prompt_text} [yes / no]: ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False

        if raw == 'yes':
            return True
        if raw in ('no', 'n', 'q', ''):
            return False
        print('  ⚠  Please type "yes" or "no".')


# ============================================================
# MAIN
# ============================================================

def run():
    _banner('delete_movement.py  —  Surgical row deletion with pallet_status recalc')
    print()
    print('  Connects to Heroku Postgres via DATABASE_URL.')
    print('  Deletes one movements row at a time; recalculates pallet_status')
    print('  for the affected HU in the same atomic transaction.')
    print()
    print('  SAFETY:')
    print('  ✓  Full row is displayed before any confirmation prompt.')
    print('  ✓  Deletion + pallet_status update are ONE transaction (atomic).')
    print('  ✓  Typing anything other than "yes" aborts — no changes made.')
    print('  ✓  ID gaps after deletion are normal — sequences are NOT renumbered.')
    _hr()

    conn = get_connection()
    print('\n  ✓  Connected to database.')

    session_deleted = 0

    while True:
        # ── Ask for row ID ───────────────────────────────────────────────
        row_id = prompt_id()
        if row_id is None:
            break

        # ── Fetch and display the row ────────────────────────────────────
        row = fetch_row(conn, row_id)
        if row is None:
            print(f'\n  ✗  No row found with id = {row_id}. Check the ID and try again.')
            continue

        print_row(row)

        hu = row.get('handling_unit', '')
        mt = row.get('movement_type', '')

        # ── Count before deletion ────────────────────────────────────────
        total_before = count_movements(conn)

        # ── Confirm ──────────────────────────────────────────────────────
        print()
        print('  ⚠   WARNING: This action is PERMANENT and cannot be undone.')
        print(f'  ⚠   Row id={row_id}  HU={hu}  type={mt} will be removed from movements.')
        print(f'  ⚠   pallet_status for {hu} will be recalculated automatically.')
        print()

        confirmed = prompt_confirm(f'Delete row {row_id}?')
        if not confirmed:
            print('  →  Skipped. No changes made.')
            # Ask whether to continue
            if not prompt_confirm('\n  Delete another row?'):
                break
            continue

        # ── Execute delete + recalculate ─────────────────────────────────
        print()
        print(f'  [DELETE]  Removing row {row_id} and recalculating pallet_status for {hu}...')

        result = delete_and_recalculate(conn, row_id, hu)

        if not result['ok']:
            print(f'\n  ✗  FAILED — transaction rolled back.')
            print(f'     Reason: {result["error"]}')
            print('     movements table is UNCHANGED.')
        else:
            session_deleted += 1
            total_after = count_movements(conn)
            ps = result['ps']

            print()
            print(f'  ✅  Row {row_id} deleted from movements.')
            print()
            _hr('─')
            print(f'  PALLET STATUS UPDATE  →  {hu}')
            _hr('─')
            if ps.get('note'):
                print(f'  New status   : {ps["status"]}  ({ps["note"]})')
            else:
                print(f'  New status   : {ps["status"]}')
                print(f'  Last movement: {ps["mv_type"]}  @  {ps["ts"]}')
                print(f'  Location     : {ps["location"]}')
            _hr('─')
            print()
            print(f'  movements table: {total_before:,} → {total_after:,} rows')
            if total_before - total_after == 1:
                print('  ✓  Count reduced by exactly 1 (expected).')
            else:
                print(f'  ⚠  Expected count to drop by 1 but dropped by {total_before - total_after}.')

        # ── Continue? ────────────────────────────────────────────────────
        print()
        if not prompt_confirm('Delete another row?'):
            break

    # ── Exit summary ─────────────────────────────────────────────────────
    print()
    _hr('═')
    if session_deleted == 0:
        print('  No rows were deleted this session.')
    else:
        print(f'  ✓  Session complete.  {session_deleted} row(s) deleted.')
        print('  ✓  All affected pallet_status rows recalculated.')
        print('  ✓  Admin dashboard will reflect changes on next load.')
    _hr('═')
    print()

    try:
        conn.close()
    except Exception:
        pass


if __name__ == '__main__':
    run()
