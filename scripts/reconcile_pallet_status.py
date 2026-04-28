"""
scripts/reconcile_pallet_status.py

Purpose
-------
Repairs the `pallet_status` table by re-deriving every HU's
`current_status` (and related fields) from the source-of-truth:
the most-recent non-ERROR row in `movements`.

The symptoms this fixes
-----------------------
  - Admin portal Pallet Status tab shows a pallet as IN_WAREHOUSE,
    but the movements table has a later STOCK_OUT for that HU.
  - Admin portal Pallet Status tab shows a pallet as OUT_OF_WAREHOUSE,
    but the movements table has a later STOCK_IN for that HU.

Root cause
----------
  These drifts happen when the `upsert_pallet_status` call inside
  `process_movement` fails silently (Postgres hiccup, Heroku dyno
  restart mid-request), OR when backfill / repair scripts insert
  new movement rows that post-date the current pallet_status row
  without updating pallet_status in the same transaction.

What this script does
---------------------
  1. For every HU in `pallet_status`, fetch the latest non-ERROR
     movement row from `movements`.
  2. Derive what `current_status` SHOULD be from that movement.
  3. Compare against what `pallet_status` currently stores.
  4. If they differ — or if any other field (location, pallet_qty,
     indus_part) is stale — flag the row for UPDATE.
  5. Show a full dry-run table of all proposed changes.
  6. Ask for explicit YES confirmation before writing.
  7. Execute all UPDATEs in a single transaction; ROLLBACK on any error.

  Additionally: if `movements` contains HUs that are NOT yet in
  `pallet_status` at all, they are INSERTed (handles HUs written by
  old migration scripts that predated pallet_status tracking).

Safety contract
---------------
  - Only `pallet_status` rows are touched.  `movements` is READ ONLY.
  - Every update is an UPSERT — no deletes ever.
  - Single atomic transaction; any failure rolls back all changes.
  - Script is idempotent — re-running it after a successful run
    produces zero diffs (nothing to update).
  - All proposed changes are printed before any write happens.

Usage
-----
    heroku run python scripts/reconcile_pallet_status.py --app indus-warehouse-tracker
    OR locally with DATABASE_URL set in your environment.
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
    uri = os.environ.get('DATABASE_URL', '')
    if not uri:
        print('ERROR: DATABASE_URL environment variable is not set.')
        sys.exit(1)
    if uri.startswith('postgres://'):
        uri = uri.replace('postgres://', 'postgresql://', 1)
    try:
        conn = psycopg2.connect(uri, cursor_factory=psycopg2.extras.RealDictCursor)
        conn.autocommit = True   # explicit BEGIN/COMMIT used for write block only
        return conn
    except psycopg2.Error as exc:
        print(f'ERROR: Cannot connect to Postgres: {exc}')
        sys.exit(1)


# ============================================================
# SQL
# ============================================================

# Fetch the ground-truth latest non-ERROR movement for every HU
# that appears in either movements OR pallet_status.
# Using a FULL OUTER JOIN so we catch:
#   - HUs in movements but missing from pallet_status (orphan INSERTs needed)
#   - HUs in pallet_status but with no movements (should be rare / impossible
#     in production, but we surface them for manual review)
FETCH_GROUND_TRUTH = """
    WITH latest_moves AS (
        SELECT DISTINCT ON (handling_unit)
            handling_unit,
            event_id,
            movement_type,
            timestamp_utc,
            indus_part,
            pallet_qty,
            location
        FROM movements
        WHERE movement_type <> 'ERROR'
        ORDER BY handling_unit, timestamp_utc DESC, id DESC
    )
    SELECT
        -- ground truth from movements
        lm.handling_unit        AS mv_hu,
        lm.event_id             AS mv_event_id,
        lm.movement_type        AS mv_movement_type,
        lm.timestamp_utc        AS mv_timestamp,
        lm.indus_part           AS mv_indus_part,
        lm.pallet_qty           AS mv_pallet_qty,
        lm.location             AS mv_location,

        -- current pallet_status values
        ps.handling_unit        AS ps_hu,
        ps.current_status       AS ps_current_status,
        ps.last_event_id        AS ps_last_event_id,
        ps.last_movement_type   AS ps_last_movement_type,
        ps.last_movement_time   AS ps_last_movement_time,
        ps.indus_part           AS ps_indus_part,
        ps.pallet_qty           AS ps_pallet_qty,
        ps.last_location        AS ps_last_location

    FROM latest_moves lm
    FULL OUTER JOIN pallet_status ps
        ON ps.handling_unit = lm.handling_unit

    ORDER BY COALESCE(lm.handling_unit, ps.handling_unit);
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
        current_status     = EXCLUDED.current_status,
        last_event_id      = EXCLUDED.last_event_id,
        last_movement_type = EXCLUDED.last_movement_type,
        last_movement_time = EXCLUDED.last_movement_time,
        indus_part         = EXCLUDED.indus_part,
        pallet_qty         = EXCLUDED.pallet_qty,
        last_location      = EXCLUDED.last_location;
"""


# ============================================================
# HELPERS
# ============================================================

def _hr(char='─', width=76):
    print(char * width)


def _banner(title):
    _hr('═')
    print(f'  {title}')
    _hr('═')


def _status_from_movement(movement_type: str) -> str:
    mt = (movement_type or '').strip().upper()
    if mt == 'STOCK_IN':
        return 'IN_WAREHOUSE'
    if mt == 'STOCK_OUT':
        return 'OUT_OF_WAREHOUSE'
    # Any other movement type (shouldn't exist but defensive)
    return 'UNKNOWN'


def _norm(val) -> str:
    """Normalise a DB value to a clean string for comparison."""
    if val is None:
        return ''
    s = str(val).strip()
    # Strip time zone suffix from timestamps for display; keep full value for writes
    return s


def _ts_str(val) -> str:
    """Return ISO string for a timestamp value (datetime or string)."""
    if val is None:
        return ''
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.isoformat()
    return str(val)


# ============================================================
# ANALYSIS
# ============================================================

def build_diff(rows: list) -> tuple[list, list, list]:
    """
    Compare movements ground truth against pallet_status.

    Returns three lists:
        updates  — rows where pallet_status needs to change
        inserts  — HUs present in movements but absent from pallet_status
        orphans  — HUs present in pallet_status but absent from movements
                   (printed as warnings, not modified)
    """
    updates = []
    inserts = []
    orphans = []

    for row in rows:
        row = dict(row)
        mv_hu  = row.get('mv_hu')
        ps_hu  = row.get('ps_hu')
        hu     = mv_hu or ps_hu

        # ── HU in pallet_status but no movements row at all ──────────────
        if mv_hu is None and ps_hu is not None:
            orphans.append({
                'hu'             : ps_hu,
                'ps_status'      : row.get('ps_current_status'),
                'ps_location'    : row.get('ps_last_location'),
                'ps_last_move'   : row.get('ps_last_movement_type'),
            })
            continue

        # ── Ground truth from movements ──────────────────────────────────
        correct_status   = _status_from_movement(row['mv_movement_type'])
        correct_event_id = row['mv_event_id']
        correct_mv_type  = (row['mv_movement_type'] or '').strip()
        correct_ts       = _ts_str(row['mv_timestamp'])
        correct_part     = (row['mv_indus_part']  or '').strip()
        correct_qty      = (row['mv_pallet_qty']  or '').strip()
        correct_location = (row['mv_location']    or '').strip().upper()

        # ── HU in movements but NOT in pallet_status at all ─────────────
        if ps_hu is None:
            inserts.append({
                'hu'             : hu,
                'reason'         : 'Missing from pallet_status entirely',
                'correct_status' : correct_status,
                'correct_location': correct_location,
                'correct_mv_type': correct_mv_type,
                'correct_event_id': correct_event_id,
                'correct_ts'     : correct_ts,
                'correct_part'   : correct_part,
                'correct_qty'    : correct_qty,
            })
            continue

        # ── Compare pallet_status against ground truth ───────────────────
        ps_status   = _norm(row.get('ps_current_status'))
        ps_event    = _norm(row.get('ps_last_event_id'))
        ps_mv_type  = _norm(row.get('ps_last_movement_type'))
        ps_part     = _norm(row.get('ps_indus_part'))
        ps_qty      = _norm(row.get('ps_pallet_qty'))
        ps_location = _norm(row.get('ps_last_location')).upper()

        reasons = []

        if ps_status != correct_status:
            reasons.append(
                f'current_status: "{ps_status}" → "{correct_status}"'
            )

        if ps_event != correct_event_id:
            reasons.append(
                f'last_event_id: "{ps_event}" → "{correct_event_id}"'
            )

        if ps_mv_type.upper() != correct_mv_type.upper():
            reasons.append(
                f'last_movement_type: "{ps_mv_type}" → "{correct_mv_type}"'
            )

        # Only flag location drift when the correct_location is non-empty
        # (avoids spurious diffs for old rows with no location stored)
        if correct_location and ps_location != correct_location:
            reasons.append(
                f'last_location: "{ps_location}" → "{correct_location}"'
            )

        if correct_part and ps_part != correct_part:
            reasons.append(
                f'indus_part: "{ps_part}" → "{correct_part}"'
            )

        if correct_qty and ps_qty != correct_qty:
            reasons.append(
                f'pallet_qty: "{ps_qty}" → "{correct_qty}"'
            )

        if reasons:
            updates.append({
                'hu'              : hu,
                'reasons'         : reasons,
                # What it will be written as:
                'correct_status'  : correct_status,
                'correct_location': correct_location,
                'correct_mv_type' : correct_mv_type,
                'correct_event_id': correct_event_id,
                'correct_ts'      : correct_ts,
                'correct_part'    : correct_part,
                'correct_qty'     : correct_qty,
                # Current values (for display only)
                'ps_status'       : ps_status,
                'ps_location'     : ps_location,
            })

    return updates, inserts, orphans


# ============================================================
# DISPLAY
# ============================================================

def print_dry_run(updates: list, inserts: list, orphans: list):
    """Print the full dry-run preview."""

    # ── Status mismatches (the primary fix) ──────────────────────────────
    status_fixes = [u for u in updates if any('current_status' in r for r in u['reasons'])]
    other_fixes  = [u for u in updates if u not in status_fixes]

    if status_fixes:
        print(f'\n  ┌─ STATUS MISMATCHES ({len(status_fixes)} pallets) ─────────────────────────────')
        print(  '  │  These pallets have a STOCK_OUT in movements but show IN_WAREHOUSE')
        print(  '  │  in pallet_status (or vice-versa).  Root cause of the tab discrepancy.')
        _hr('─')
        print(f'  {"HU":<22}  {"NOW":>18}  {"WILL BE":>18}  {"LOCATION"}')
        _hr('─')
        for u in status_fixes:
            arrow = f'{u["ps_status"]:>18}  →  {u["correct_status"]:>18}'
            print(f'  {u["hu"]:<22}  {arrow}  {u["correct_location"]}')
        _hr('─')

    if other_fixes:
        print(f'\n  ┌─ OTHER FIELD DRIFTS ({len(other_fixes)} pallets) ────────────────────────────────')
        print(  '  │  event_id / movement_type / location / part / qty out of sync.')
        _hr('─')
        for u in other_fixes:
            print(f'  {u["hu"]}')
            for r in u['reasons']:
                print(f'      {r}')
        _hr('─')

    if inserts:
        print(f'\n  ┌─ MISSING FROM pallet_status ({len(inserts)} pallets) ─────────────────────────')
        print(  '  │  These HUs exist in movements but have no pallet_status row at all.')
        _hr('─')
        for ins in inserts:
            print(f'  {ins["hu"]:<22}  {ins["correct_status"]:>18}  @ {ins["correct_location"]}')
        _hr('─')

    if orphans:
        print(f'\n  ┌─ ORPHAN pallet_status ROWS ({len(orphans)} pallets) ─ WARNING ─────────────────')
        print(  '  │  These HUs are in pallet_status but have ZERO movement rows.')
        print(  '  │  They are NOT modified by this script — review manually.')
        _hr('─')
        for o in orphans:
            print(f'  {o["hu"]:<22}  status={o["ps_status"]}  loc={o["ps_location"]}')
        _hr('─')


# ============================================================
# EXECUTION
# ============================================================

def execute_reconciliation(conn, updates: list, inserts: list):
    """
    Write all pallet_status corrections in a single transaction.
    Returns (written_count, rolled_back: bool).
    """
    all_rows = updates + inserts
    written  = 0
    cur = conn.cursor()

    try:
        cur.execute('BEGIN')

        for item in all_rows:
            hu = item['hu']
            params = {
                'handling_unit'     : hu,
                'current_status'    : item['correct_status'],
                'last_event_id'     : item['correct_event_id'],
                'last_movement_type': item['correct_mv_type'],
                'last_movement_time': item['correct_ts'],
                'indus_part'        : item['correct_part'],
                'pallet_qty'        : item['correct_qty'],
                'last_location'     : item['correct_location'],
            }
            cur.execute(UPSERT_PALLET_STATUS, params)
            written += 1

            status_arrow = (
                f'{item["ps_status"]:>18} → {item["correct_status"]}'
                if 'ps_status' in item else item['correct_status']
            )
            print(f'  ✓  {hu:<22}  {status_arrow}')

        cur.execute('COMMIT')
        return written, False

    except Exception as exc:
        cur.execute('ROLLBACK')
        print(f'\n  ✗  ERROR during write — full ROLLBACK applied.')
        print(f'     Exception: {exc}')
        return 0, True
    finally:
        cur.close()


# ============================================================
# MAIN
# ============================================================

def run():
    _banner('reconcile_pallet_status.py')
    print()
    print('  PURPOSE: Re-derive pallet_status from movements (source of truth).')
    print('  Fixes:   Pallets showing IN_WAREHOUSE when STOCK_OUT already recorded.')
    print('           Pallets showing OUT_OF_WAREHOUSE when STOCK_IN already recorded.')
    print()
    print('  SAFETY:')
    print('  ✓  movements table is READ ONLY — zero rows touched there.')
    print('  ✓  Only pallet_status is written (UPSERT — no deletes).')
    print('  ✓  All changes in one transaction — any error = full ROLLBACK.')
    print('  ✓  Full dry-run preview before any write.')
    print('  ✓  Idempotent — safe to re-run; already-correct rows are skipped.')
    _hr()

    conn = get_connection()
    print('  ✓  Connected to database.')
    print()

    # ── Fetch ground truth ───────────────────────────────────────────────
    print('[SCAN] Comparing movements ↔ pallet_status for all HUs...')
    with conn.cursor() as cur:
        cur.execute(FETCH_GROUND_TRUTH)
        rows = list(cur.fetchall())

    print(f'  Rows analysed: {len(rows)}')

    updates, inserts, orphans = build_diff(rows)

    total_changes = len(updates) + len(inserts)

    if total_changes == 0 and not orphans:
        print()
        print('  ✓  pallet_status is fully consistent with movements. Nothing to fix.')
        conn.close()
        sys.exit(0)

    # ── Dry-run output ───────────────────────────────────────────────────
    print()
    _hr()
    print('  DRY-RUN PREVIEW — proposed pallet_status changes:')
    _hr()
    print_dry_run(updates, inserts, orphans)

    print()
    _hr()
    print('  SUMMARY:')
    print(f'    Status + field corrections (UPDATE)  : {len(updates)}')
    print(f'    Missing pallet_status rows (INSERT)  : {len(inserts)}')
    print(f'    Orphan pallet_status rows (WARNING)  : {len(orphans)}  ← not modified')
    print(f'    movements table writes               : 0  ← guaranteed')
    _hr()

    if total_changes == 0:
        print()
        print('  No pallet_status writes needed (orphan warnings only).')
        conn.close()
        sys.exit(0)

    answer = input(
        f'\n  Proceed with writing {total_changes} pallet_status correction(s)?'
        f' [type YES to confirm, anything else to abort]: '
    ).strip()

    if answer.upper() != 'YES':
        print('\n  Aborted. No changes made.')
        conn.close()
        sys.exit(0)

    # ── Execute ──────────────────────────────────────────────────────────
    print()
    _hr()
    print('  Writing corrections...')
    _hr()

    written, rolled_back = execute_reconciliation(conn, updates, inserts)

    print()
    _banner('COMPLETE')
    if rolled_back:
        print('  ✗  Transaction was ROLLED BACK due to an error.')
        print('     No changes were made. Re-run to retry.')
    else:
        print(f'  ✓  {written} pallet_status row(s) corrected.')
        print(f'  ✓  {len(orphans)} orphan row(s) left unchanged (review manually).')
        print()
        print('  VERIFY in admin portal → Pallet Status tab.')
        print('  AUDIT query:')
        print("    SELECT ps.handling_unit, ps.current_status, ps.last_movement_type,")
        print("           ps.last_location, ps.last_movement_time")
        print("    FROM   pallet_status ps")
        print("    JOIN   (SELECT DISTINCT ON (handling_unit)")
        print("              handling_unit, movement_type, timestamp_utc, location")
        print("              FROM movements WHERE movement_type <> 'ERROR'")
        print("              ORDER BY handling_unit, timestamp_utc DESC, id DESC")
        print("           ) lm ON lm.handling_unit = ps.handling_unit")
        print("    WHERE  ps.current_status <> CASE lm.movement_type")
        print("               WHEN 'STOCK_IN'  THEN 'IN_WAREHOUSE'")
        print("               WHEN 'STOCK_OUT' THEN 'OUT_OF_WAREHOUSE'")
        print("           END;")
        print("    -- Should return 0 rows after a successful reconciliation.")
    _hr('═')

    conn.close()


if __name__ == '__main__':
    run()
