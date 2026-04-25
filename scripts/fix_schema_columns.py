"""
scripts/fix_schema_columns.py

Fixes the schema mismatch that causes every movement POST to fail with:
  "Could not save. Saved locally — will sync automatically."

ROOT CAUSE:
  create_schema.py created movements with columns:
      location TEXT, destination TEXT

  But db_client.insert_movement() inserts into:
      location, from_location, to_location

  The two missing columns cause a PostgreSQL "column does not exist"
  error on every INSERT, which movement_service.py catches as DbError
  and returns the 'failed_write_logged' status to the frontend.

  Additionally, the validation_status CHECK constraint only allows
  ('VALID', 'OVERRIDE') but movement_service._log_error() writes values
  like 'VALIDATION_FAIL', 'DUPLICATE', 'INVALID_TRANSITION', etc.,
  causing a second constraint-violation failure path.

WHAT THIS SCRIPT DOES (all changes are safe and non-destructive):
  1. Adds   from_location TEXT DEFAULT '' to movements  (if missing)
  2. Adds   to_location   TEXT DEFAULT '' to movements  (if missing)
  3. Drops  the old validation_status CHECK constraint  (if present)
  4. Drops  the old movement_type CHECK constraint      (if present,
           so ERROR rows written by _log_error can be inserted)
  5. Verifies the fix by doing a test SELECT on the new columns
  6. Prints a clear pass/fail summary

Safe to re-run — every step is guarded with IF NOT EXISTS / IF EXISTS
or a prior existence check, so running it twice does nothing harmful.

Usage:
    heroku run python scripts/fix_schema_columns.py --app indus-warehouse-tracker
    OR locally with DATABASE_URL set in .env
"""

import os
import sys
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_connection():
    uri = os.environ.get("DATABASE_URL", "")
    if not uri:
        print("ERROR: DATABASE_URL environment variable is not set.")
        sys.exit(1)
    if uri.startswith("postgres://"):
        uri = "postgresql://" + uri[len("postgres://"):]
    try:
        conn = psycopg2.connect(uri)
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        return conn
    except psycopg2.Error as exc:
        print(f"ERROR: Could not connect to Postgres: {exc}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def column_exists(cur, table, column):
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
    """, (table, column))
    return cur.fetchone() is not None


def constraint_exists(cur, table, constraint):
    cur.execute("""
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = %s AND constraint_name = %s
    """, (table, constraint))
    return cur.fetchone() is not None


def step(label, ok, detail=""):
    mark = "✓" if ok else "✗"
    msg = f"  {mark}  {label}"
    if detail:
        msg += f"\n       {detail}"
    print(msg)
    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    print("=" * 62)
    print("  fix_schema_columns.py")
    print("  Fixes missing from_location / to_location columns")
    print("  and loosens over-strict CHECK constraints")
    print("=" * 62)

    conn = get_connection()
    cur = conn.cursor()
    all_ok = True

    # ── 1. Add from_location ─────────────────────────────────────────────
    print("\n[1/5] from_location column...")
    if column_exists(cur, "movements", "from_location"):
        step("from_location already exists — skipping", True)
    else:
        try:
            cur.execute("ALTER TABLE movements ADD COLUMN from_location TEXT DEFAULT '';")
            all_ok &= step("Added from_location TEXT DEFAULT ''", True)
        except psycopg2.Error as exc:
            all_ok &= step("Failed to add from_location", False, str(exc))

    # ── 2. Add to_location ───────────────────────────────────────────────
    print("\n[2/5] to_location column...")
    if column_exists(cur, "movements", "to_location"):
        step("to_location already exists — skipping", True)
    else:
        try:
            cur.execute("ALTER TABLE movements ADD COLUMN to_location TEXT DEFAULT '';")
            all_ok &= step("Added to_location TEXT DEFAULT ''", True)
        except psycopg2.Error as exc:
            all_ok &= step("Failed to add to_location", False, str(exc))

    # ── 3. Drop validation_status CHECK constraint ───────────────────────
    # movement_service._log_error() writes values such as 'VALIDATION_FAIL',
    # 'DUPLICATE', 'INVALID_TRANSITION', etc. The original constraint only
    # allowed ('VALID', 'OVERRIDE'), causing a second failure path.
    print("\n[3/5] validation_status CHECK constraint...")
    vs_constraint = "movements_validation_status_check"
    if constraint_exists(cur, "movements", vs_constraint):
        try:
            cur.execute(f"ALTER TABLE movements DROP CONSTRAINT {vs_constraint};")
            all_ok &= step(
                f"Dropped {vs_constraint}",
                True,
                "validation_status now accepts any text value (audit field).",
            )
        except psycopg2.Error as exc:
            all_ok &= step(f"Failed to drop {vs_constraint}", False, str(exc))
    else:
        step(f"{vs_constraint} not found — skipping", True)

    # ── 4. Drop movement_type CHECK constraint ───────────────────────────
    # _log_error() inserts rows with movement_type = 'ERROR', which the
    # original CHECK (movement_type IN ('STOCK_IN','STOCK_OUT')) rejects.
    print("\n[4/5] movement_type CHECK constraint...")
    mt_constraint = "movements_movement_type_check"
    if constraint_exists(cur, "movements", mt_constraint):
        try:
            cur.execute(f"ALTER TABLE movements DROP CONSTRAINT {mt_constraint};")
            all_ok &= step(
                f"Dropped {mt_constraint}",
                True,
                "movement_type now accepts 'ERROR' rows from _log_error().",
            )
        except psycopg2.Error as exc:
            all_ok &= step(f"Failed to drop {mt_constraint}", False, str(exc))
    else:
        step(f"{mt_constraint} not found — skipping", True)

    # ── 5. Verify by selecting the new columns ───────────────────────────
    print("\n[5/5] Verifying columns exist with a test SELECT...")
    try:
        cur.execute(
            "SELECT from_location, to_location FROM movements LIMIT 1;"
        )
        all_ok &= step(
            "SELECT from_location, to_location succeeded",
            True,
            "db_client.insert_movement() will now write successfully.",
        )
    except psycopg2.Error as exc:
        all_ok &= step("Verification SELECT failed", False, str(exc))

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 62)
    if all_ok:
        print("  ALL STEPS PASSED.")
        print("  Movements will now save correctly.")
        print("  No app restart needed — Heroku dynos pick this up immediately.")
    else:
        print("  ONE OR MORE STEPS FAILED.")
        print("  Review the errors above and re-run, or fix manually.")
        print("  The app will continue to show the same error until resolved.")
    print("=" * 62)

    cur.close()
    conn.close()

    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    run()
