"""
scripts/fix_migrated_dates.py

ONE-TIME fix for migrated timestamps where DD.MM.YY (e.g. 12.4.26 = 12 Apr 2026)
was read as MM/DD/YY (e.g. 4/12/26 = 4 Dec 2026), swapping day and month.

Affected rows: any timestamp_utc that falls between 2026-12-01 and 2026-12-12
(day 13+ is impossible after a DD↔MM swap since no month has a 13th month).

Tables fixed:
  - movements.timestamp_utc
  - pallet_status.last_movement_time

The fix swaps month ↔ day back to the correct April date while preserving
the original wall-clock time in Dubai timezone exactly.

Usage:
    heroku run python scripts/fix_migrated_dates.py --app indus-warehouse-tracker

    OR locally with DATABASE_URL set:
    python scripts/fix_migrated_dates.py

    To REVERSE (undo) the fix, add --reverse flag:
    heroku run python scripts/fix_migrated_dates.py --reverse --app indus-warehouse-tracker

SAFE GUARDS:
  - Prints a full preview of affected rows BEFORE making any changes.
  - Requires typing 'yes' to confirm before committing.
  - Runs both tables in a single transaction — rolls back entirely if anything fails.
  - --reverse flag re-runs the same swap logic on the now-corrected April rows,
    returning them to their (wrong) December state if you need to undo.
"""

import os
import sys
import psycopg2
import psycopg2.extras


# ── Connection ─────────────────────────────────────────────────────────────

def get_connection():
    uri = os.environ.get("DATABASE_URL")
    if not uri:
        print("ERROR: DATABASE_URL environment variable is not set.")
        sys.exit(1)
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(uri, cursor_factory=psycopg2.extras.RealDictCursor)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _where_clause(reverse: bool) -> tuple[str, dict]:
    """
    Return the WHERE clause and params that target the corrupted rows.

    Forward pass:  targets Dec 2026 rows  (the wrongly-migrated dates)
    Reverse pass:  targets Apr 2026 rows  (the corrected dates, to undo)
    """
    if not reverse:
        # Corrupted rows landed in December 2026 (month=12, day=1-12)
        return (
            "timestamp_utc >= %(start)s AND timestamp_utc < %(end)s",
            {"start": "2026-12-01", "end": "2026-12-13"},
        )
    else:
        # Corrected rows are in April 2026 (month=4, day=1-12)
        return (
            "timestamp_utc >= %(start)s AND timestamp_utc < %(end)s",
            {"start": "2026-04-01", "end": "2026-04-13"},
        )


SWAP_EXPR = """
    make_timestamptz(
        EXTRACT(YEAR   FROM timestamp_utc)::int,
        EXTRACT(DAY    FROM timestamp_utc)::int,
        EXTRACT(MONTH  FROM timestamp_utc)::int,
        EXTRACT(HOUR   FROM (timestamp_utc AT TIME ZONE 'Asia/Dubai'))::int,
        EXTRACT(MINUTE FROM (timestamp_utc AT TIME ZONE 'Asia/Dubai'))::int,
        EXTRACT(SECOND FROM (timestamp_utc AT TIME ZONE 'Asia/Dubai')),
        'Asia/Dubai'
    )
"""

SWAP_EXPR_PS = """
    make_timestamptz(
        EXTRACT(YEAR   FROM last_movement_time)::int,
        EXTRACT(DAY    FROM last_movement_time)::int,
        EXTRACT(MONTH  FROM last_movement_time)::int,
        EXTRACT(HOUR   FROM (last_movement_time AT TIME ZONE 'Asia/Dubai'))::int,
        EXTRACT(MINUTE FROM (last_movement_time AT TIME ZONE 'Asia/Dubai'))::int,
        EXTRACT(SECOND FROM (last_movement_time AT TIME ZONE 'Asia/Dubai')),
        'Asia/Dubai'
    )
"""


# ── Preview ───────────────────────────────────────────────────────────────────

def preview_movements(cur, where: str, params: dict) -> int:
    sql = f"""
        SELECT
            id,
            handling_unit,
            movement_type,
            timestamp_utc AT TIME ZONE 'Asia/Dubai'  AS current_dubai,
            ({SWAP_EXPR}) AT TIME ZONE 'Asia/Dubai'  AS corrected_dubai
        FROM movements
        WHERE {where}
        ORDER BY timestamp_utc
    """
    cur.execute(sql, params)
    rows = cur.fetchall()
    if not rows:
        print("  (no rows matched in movements)")
        return 0
    print(f"\n  {'ID':<6}  {'Handling Unit':<22}  {'Type':<10}  {'CURRENT (Dubai)':<28}  {'CORRECTED (Dubai)'}")
    print("  " + "-" * 100)
    for r in rows:
        print(
            f"  {r['id']:<6}  {r['handling_unit']:<22}  {r['movement_type']:<10}  "
            f"{str(r['current_dubai']):<28}  {r['corrected_dubai']}"
        )
    return len(rows)


def preview_pallet_status(cur, reverse: bool) -> int:
    # pallet_status uses last_movement_time, need its own where clause
    if not reverse:
        where = "last_movement_time >= '2026-12-01' AND last_movement_time < '2026-12-13'"
    else:
        where = "last_movement_time >= '2026-04-01' AND last_movement_time < '2026-04-13'"

    sql = f"""
        SELECT
            handling_unit,
            last_movement_time AT TIME ZONE 'Asia/Dubai'   AS current_dubai,
            ({SWAP_EXPR_PS}) AT TIME ZONE 'Asia/Dubai'     AS corrected_dubai
        FROM pallet_status
        WHERE {where}
        ORDER BY last_movement_time
    """
    cur.execute(sql)
    rows = cur.fetchall()
    if not rows:
        print("  (no rows matched in pallet_status)")
        return 0
    print(f"\n  {'Handling Unit':<24}  {'CURRENT (Dubai)':<28}  {'CORRECTED (Dubai)'}")
    print("  " + "-" * 82)
    for r in rows:
        print(
            f"  {r['handling_unit']:<24}  "
            f"{str(r['current_dubai']):<28}  {r['corrected_dubai']}"
        )
    return len(rows)


# ── Fix ───────────────────────────────────────────────────────────────────────

def apply_fix_movements(cur, where: str, params: dict) -> int:
    sql = f"""
        UPDATE movements
        SET timestamp_utc = {SWAP_EXPR}
        WHERE {where}
    """
    cur.execute(sql, params)
    return cur.rowcount


def apply_fix_pallet_status(cur, reverse: bool) -> int:
    if not reverse:
        where = "last_movement_time >= '2026-12-01' AND last_movement_time < '2026-12-13'"
    else:
        where = "last_movement_time >= '2026-04-01' AND last_movement_time < '2026-04-13'"

    sql = f"""
        UPDATE pallet_status
        SET last_movement_time = {SWAP_EXPR_PS}
        WHERE {where}
    """
    cur.execute(sql)
    return cur.rowcount


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    reverse = "--reverse" in sys.argv
    mode = "REVERSE (undoing the fix — restoring Dec 2026 dates)" if reverse else "FORWARD (fixing Dec 2026 → Apr 2026)"

    print("=" * 70)
    print(f"  fix_migrated_dates.py  —  mode: {mode}")
    print("=" * 70)

    conn = get_connection()
    cur = conn.cursor()

    # ── Step 1: Preview ───────────────────────────────────────────────────
    where, params = _where_clause(reverse)

    print("\n[1/3]  PREVIEW — rows that will be changed\n")
    print("  movements table:")
    mv_count = preview_movements(cur, where, params)

    print("\n  pallet_status table:")
    ps_count = preview_pallet_status(cur, reverse)

    total = mv_count + ps_count
    print(f"\n  Total rows affected: {total}  ({mv_count} movements + {ps_count} pallet_status)")

    if total == 0:
        print("\nNothing to fix. Exiting without changes.")
        cur.close()
        conn.close()
        sys.exit(0)

    # ── Step 2: Confirm ───────────────────────────────────────────────────
    print("\n[2/3]  CONFIRM")
    answer = input(f"\n  Type 'yes' to apply the {mode} to all {total} rows: ").strip().lower()
    if answer != "yes":
        print("  Aborted. No changes made.")
        cur.close()
        conn.close()
        sys.exit(0)

    # ── Step 3: Apply in single transaction ───────────────────────────────
    print("\n[3/3]  APPLYING CHANGES ...")
    try:
        mv_updated = apply_fix_movements(cur, where, params)
        print(f"  ✓  movements      — {mv_updated} rows updated")

        ps_updated = apply_fix_pallet_status(cur, reverse)
        print(f"  ✓  pallet_status  — {ps_updated} rows updated")

        conn.commit()
        print(f"\n  Committed. {mv_updated + ps_updated} rows updated successfully.")

        if not reverse:
            print("\n  To UNDO this fix at any time, run:")
            print("    heroku run python scripts/fix_migrated_dates.py --reverse --app indus-warehouse-tracker")

    except Exception as exc:
        conn.rollback()
        print(f"\n  ERROR: {exc}")
        print("  Transaction rolled back. No changes were made.")
        cur.close()
        conn.close()
        sys.exit(1)

    cur.close()
    conn.close()


if __name__ == "__main__":
    run()
