"""
scripts/fix_migrated_dates.py

Generalised fix for migrated timestamps where dates were written as DD.MM.YY
but read as MM/DD/YY, swapping day and month.

  Example corruptions:
    12.4.26  (12 Apr)  →  stored as  4/12/26  (4 Dec)
    10.4.26  (10 Apr)  →  stored as  4/10/26  (4 Oct)
    11.4.26  (11 Apr)  →  stored as  4/11/26  (4 Nov)
     1.4.26  ( 1 Apr)  →  stored as  4/01/26  (4 Jan)

The pattern for a source month of M:
  Corrupted rows have  local_day = M  and  local_month = original_day  (1–12)
  so the fix is simply: swap local_month ↔ local_day back.

Tables fixed:
  - movements.timestamp_utc
  - pallet_status.last_movement_time

Dubai wall-clock time (hour/minute/second) is always preserved exactly.

──────────────────────────────────────────────────────────────────────────────
USAGE
──────────────────────────────────────────────────────────────────────────────

  Preview corrupted rows for source month April (default, no writes):
    heroku run python scripts/fix_migrated_dates.py --month 4 --app indus-warehouse-tracker

  Apply the fix:
    heroku run python scripts/fix_migrated_dates.py --month 4 --apply --app indus-warehouse-tracker

  Preview what a reverse/undo would touch:
    heroku run python scripts/fix_migrated_dates.py --month 4 --reverse --app indus-warehouse-tracker

  Apply the reverse (undo the fix):
    heroku run python scripts/fix_migrated_dates.py --month 4 --reverse --apply --app indus-warehouse-tracker

  Locally (DATABASE_URL must be set in .env):
    python scripts/fix_migrated_dates.py --month 4
    python scripts/fix_migrated_dates.py --month 4 --apply

──────────────────────────────────────────────────────────────────────────────
SAFE GUARDS
──────────────────────────────────────────────────────────────────────────────
  - Default is PREVIEW ONLY — no writes happen unless --apply is passed.
  - Preview prints every affected row with current and corrected timestamps
    before any change is made.
  - Requires typing 'yes' to confirm even when --apply is passed.
  - Both tables are updated inside a single transaction; any failure rolls
    everything back leaving zero partial changes.
  - --reverse undoes exactly what --apply did; it is symmetric.
"""

import os
import sys
import argparse
import psycopg2
import psycopg2.extras


TIMEZONE = "Asia/Dubai"


# ── Connection ─────────────────────────────────────────────────────────────

def get_connection():
    uri = os.environ.get("DATABASE_URL")
    if not uri:
        print("ERROR: DATABASE_URL environment variable is not set.")
        sys.exit(1)
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(uri, cursor_factory=psycopg2.extras.RealDictCursor)


# ── SQL helpers ────────────────────────────────────────────────────────────
#
# The corruption pattern for source month M:
#   Forward (fix):   current local_day = M,  local_month in 1..12
#   Reverse (undo):  current local_month = M, local_day in 1..12
#
# In both cases the repair is identical: swap local_month ↔ local_day.
# We always extract date components in Dubai local time, then rebuild via
# make_timestamptz(..., 'Asia/Dubai') so the wall-clock hour/min/sec is
# untouched.

def _movements_where(source_month: int, reverse: bool) -> str:
    if not reverse:
        # corrupted: day-in-local = source_month, month can be 1–12
        return f"""
            EXTRACT(DAY   FROM (timestamp_utc AT TIME ZONE '{TIMEZONE}'))::int = {source_month}
            AND EXTRACT(MONTH FROM (timestamp_utc AT TIME ZONE '{TIMEZONE}'))::int BETWEEN 1 AND 12
            AND EXTRACT(YEAR  FROM (timestamp_utc AT TIME ZONE '{TIMEZONE}'))::int = 2026
        """
    else:
        # corrected: month-in-local = source_month, day can be 1–12
        return f"""
            EXTRACT(MONTH FROM (timestamp_utc AT TIME ZONE '{TIMEZONE}'))::int = {source_month}
            AND EXTRACT(DAY   FROM (timestamp_utc AT TIME ZONE '{TIMEZONE}'))::int BETWEEN 1 AND 12
            AND EXTRACT(YEAR  FROM (timestamp_utc AT TIME ZONE '{TIMEZONE}'))::int = 2026
        """


def _pallet_where(source_month: int, reverse: bool) -> str:
    if not reverse:
        return f"""
            EXTRACT(DAY   FROM (last_movement_time AT TIME ZONE '{TIMEZONE}'))::int = {source_month}
            AND EXTRACT(MONTH FROM (last_movement_time AT TIME ZONE '{TIMEZONE}'))::int BETWEEN 1 AND 12
            AND EXTRACT(YEAR  FROM (last_movement_time AT TIME ZONE '{TIMEZONE}'))::int = 2026
        """
    else:
        return f"""
            EXTRACT(MONTH FROM (last_movement_time AT TIME ZONE '{TIMEZONE}'))::int = {source_month}
            AND EXTRACT(DAY   FROM (last_movement_time AT TIME ZONE '{TIMEZONE}'))::int BETWEEN 1 AND 12
            AND EXTRACT(YEAR  FROM (last_movement_time AT TIME ZONE '{TIMEZONE}'))::int = 2026
        """


def _swap_expr_movements() -> str:
    """Build a make_timestamptz expression that swaps month ↔ day for movements."""
    return f"""
        make_timestamptz(
            EXTRACT(YEAR   FROM (timestamp_utc AT TIME ZONE '{TIMEZONE}'))::int,
            EXTRACT(DAY    FROM (timestamp_utc AT TIME ZONE '{TIMEZONE}'))::int,
            EXTRACT(MONTH  FROM (timestamp_utc AT TIME ZONE '{TIMEZONE}'))::int,
            EXTRACT(HOUR   FROM (timestamp_utc AT TIME ZONE '{TIMEZONE}'))::int,
            EXTRACT(MINUTE FROM (timestamp_utc AT TIME ZONE '{TIMEZONE}'))::int,
            EXTRACT(SECOND FROM (timestamp_utc AT TIME ZONE '{TIMEZONE}')),
            '{TIMEZONE}'
        )
    """


def _swap_expr_pallet() -> str:
    """Build a make_timestamptz expression that swaps month ↔ day for pallet_status."""
    return f"""
        make_timestamptz(
            EXTRACT(YEAR   FROM (last_movement_time AT TIME ZONE '{TIMEZONE}'))::int,
            EXTRACT(DAY    FROM (last_movement_time AT TIME ZONE '{TIMEZONE}'))::int,
            EXTRACT(MONTH  FROM (last_movement_time AT TIME ZONE '{TIMEZONE}'))::int,
            EXTRACT(HOUR   FROM (last_movement_time AT TIME ZONE '{TIMEZONE}'))::int,
            EXTRACT(MINUTE FROM (last_movement_time AT TIME ZONE '{TIMEZONE}'))::int,
            EXTRACT(SECOND FROM (last_movement_time AT TIME ZONE '{TIMEZONE}')),
            '{TIMEZONE}'
        )
    """


# ── Preview ────────────────────────────────────────────────────────────────

def preview_movements(cur, where: str) -> int:
    swap = _swap_expr_movements()
    sql = f"""
        SELECT
            id,
            handling_unit,
            movement_type,
            (timestamp_utc AT TIME ZONE '{TIMEZONE}')  AS current_local,
            ({swap})         AT TIME ZONE '{TIMEZONE}'  AS corrected_local
        FROM movements
        WHERE {where}
        ORDER BY timestamp_utc
    """
    cur.execute(sql)
    rows = cur.fetchall()
    if not rows:
        print("  (no rows matched in movements)")
        return 0

    hdr = f"  {'ID':<8}  {'Handling Unit':<24}  {'Type':<12}  {'CURRENT (Dubai)':<30}  CORRECTED (Dubai)"
    print(hdr)
    print("  " + "-" * 105)
    for r in rows:
        print(
            f"  {r['id']:<8}  {r['handling_unit']:<24}  {r['movement_type']:<12}  "
            f"{str(r['current_local']):<30}  {r['corrected_local']}"
        )
    return len(rows)


def preview_pallet_status(cur, where: str) -> int:
    swap = _swap_expr_pallet()
    sql = f"""
        SELECT
            handling_unit,
            (last_movement_time AT TIME ZONE '{TIMEZONE}')  AS current_local,
            ({swap})            AT TIME ZONE '{TIMEZONE}'    AS corrected_local
        FROM pallet_status
        WHERE {where}
        ORDER BY last_movement_time
    """
    cur.execute(sql)
    rows = cur.fetchall()
    if not rows:
        print("  (no rows matched in pallet_status)")
        return 0

    hdr = f"  {'Handling Unit':<26}  {'CURRENT (Dubai)':<30}  CORRECTED (Dubai)"
    print(hdr)
    print("  " + "-" * 88)
    for r in rows:
        print(
            f"  {r['handling_unit']:<26}  "
            f"{str(r['current_local']):<30}  {r['corrected_local']}"
        )
    return len(rows)


# ── Apply ──────────────────────────────────────────────────────────────────

def apply_movements(cur, where: str) -> int:
    swap = _swap_expr_movements()
    cur.execute(f"UPDATE movements SET timestamp_utc = {swap} WHERE {where}")
    return cur.rowcount


def apply_pallet_status(cur, where: str) -> int:
    swap = _swap_expr_pallet()
    cur.execute(f"UPDATE pallet_status SET last_movement_time = {swap} WHERE {where}")
    return cur.rowcount


# ── Main ───────────────────────────────────────────────────────────────────

def run():
    parser = argparse.ArgumentParser(
        description="Fix day/month-swapped timestamps from Excel migration."
    )
    parser.add_argument(
        "--month", type=int, default=4,
        help="Source month (the correct month the dates should be in). Default: 4 (April)."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply the fix. Without this flag the script is preview-only."
    )
    parser.add_argument(
        "--reverse", action="store_true",
        help="Reverse/undo a previously applied fix for this source month."
    )

    # heroku run passes extra args like --app; ignore unknown ones
    args, _ = parser.parse_known_args()

    source_month = args.month
    apply        = args.apply
    reverse      = args.reverse

    month_name = {
        1:"January", 2:"February", 3:"March", 4:"April",
        5:"May", 6:"June", 7:"July", 8:"August",
        9:"September", 10:"October", 11:"November", 12:"December"
    }.get(source_month, str(source_month))

    direction = "REVERSE (undo)" if reverse else "FORWARD (fix)"
    action    = "APPLY"          if apply   else "PREVIEW ONLY"

    print("=" * 70)
    print(f"  fix_migrated_dates.py")
    print(f"  Source month : {month_name} ({source_month})")
    print(f"  Direction    : {direction}")
    print(f"  Mode         : {action}")
    print("=" * 70)

    if not apply:
        print("\n  *** PREVIEW ONLY — no changes will be made ***")
        print("  *** Re-run with --apply to commit changes.  ***\n")

    conn = get_connection()
    cur  = conn.cursor()

    mv_where = _movements_where(source_month, reverse)
    ps_where = _pallet_where(source_month, reverse)

    # ── Step 1: Preview ──────────────────────────────────────────────────
    print("\n[1/3]  PREVIEW — rows that will be changed\n")
    print("  movements table:")
    mv_count = preview_movements(cur, mv_where)

    print("\n  pallet_status table:")
    ps_count = preview_pallet_status(cur, ps_where)

    total = mv_count + ps_count
    print(f"\n  Total rows matched: {total}  ({mv_count} movements + {ps_count} pallet_status)")

    if total == 0:
        print("\n  Nothing to change. Exiting without any database writes.")
        cur.close(); conn.close(); sys.exit(0)

    if not apply:
        print("\n  Preview complete. Run with --apply to commit these changes.")
        cur.close(); conn.close(); sys.exit(0)

    # ── Step 2: Confirm ──────────────────────────────────────────────────
    print("\n[2/3]  CONFIRM")
    answer = input(
        f"\n  Type 'yes' to {direction.lower()} {total} rows: "
    ).strip().lower()
    if answer != "yes":
        print("  Aborted. No changes made.")
        cur.close(); conn.close(); sys.exit(0)

    # ── Step 3: Apply in single transaction ──────────────────────────────
    print("\n[3/3]  APPLYING CHANGES ...")
    try:
        mv_updated = apply_movements(cur, mv_where)
        print(f"  ✓  movements      — {mv_updated} rows updated")

        ps_updated = apply_pallet_status(cur, ps_where)
        print(f"  ✓  pallet_status  — {ps_updated} rows updated")

        conn.commit()
        print(f"\n  Committed. {mv_updated + ps_updated} rows updated successfully.")

        if not reverse:
            print(f"\n  To UNDO this fix, run:")
            print(f"    heroku run python scripts/fix_migrated_dates.py --month {source_month} --reverse --apply --app indus-warehouse-tracker")
        else:
            print(f"\n  Reverse complete. Rows are back to their pre-fix state.")

    except Exception as exc:
        conn.rollback()
        print(f"\n  ERROR: {exc}")
        print("  Transaction rolled back. No changes were made.")
        cur.close(); conn.close(); sys.exit(1)

    cur.close()
    conn.close()


if __name__ == "__main__":
    run()
