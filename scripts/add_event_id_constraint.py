"""
scripts/add_event_id_constraint.py

Adds a UNIQUE constraint on movements.event_id so that
`ON CONFLICT (event_id) DO NOTHING` in db_client.insert_movement()
works correctly for idempotency / offline-sync deduplication.

Safe to run on a live database with existing data:
  - Uses IF NOT EXISTS so it is a no-op if the constraint already exists.
  - Does NOT modify, delete, or re-index any existing rows.
  - All historical migrated rows are preserved exactly as-is.
  - Only NEW movements scanned going forward will be subject to the
    uniqueness check (each new movement gets a fresh uuid4 event_id,
    so there will never be a collision with live data).

Before running, Postgres must have no duplicate event_id values in
the movements table (duplicates would cause the ALTER TABLE to fail).
The script checks for duplicates first and aborts cleanly if found,
printing the offending values so you can decide how to handle them.

Usage:
    heroku run python scripts/add_event_id_constraint.py --app indus-warehouse-tracker
    OR locally with DATABASE_URL set in your .env / environment.
"""

import os
import sys

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT


# ── Connection ────────────────────────────────────────────────────────────────

def get_connection():
    uri = os.environ.get("DATABASE_URL", "")
    if not uri:
        print("ERROR: DATABASE_URL environment variable is not set.")
        sys.exit(1)
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    try:
        return psycopg2.connect(uri)
    except psycopg2.Error as exc:
        print(f"ERROR: Could not connect to Postgres: {exc}")
        sys.exit(1)


# ── Checks ────────────────────────────────────────────────────────────────────

CHECK_CONSTRAINT_EXISTS = """
    SELECT 1
    FROM   information_schema.table_constraints
    WHERE  table_name       = 'movements'
    AND    constraint_type  = 'UNIQUE'
    AND    constraint_name  = 'movements_event_id_key';
"""

CHECK_DUPLICATES = """
    SELECT event_id, COUNT(*) AS cnt
    FROM   movements
    GROUP  BY event_id
    HAVING COUNT(*) > 1
    ORDER  BY cnt DESC
    LIMIT  20;
"""

ADD_CONSTRAINT = """
    ALTER TABLE movements
    ADD CONSTRAINT movements_event_id_key UNIQUE (event_id);
"""

DROP_OLD_INDEX = """
    DROP INDEX IF EXISTS idx_movements_event_id;
"""

# A UNIQUE constraint on Postgres automatically creates a unique index,
# so the plain non-unique index idx_movements_event_id becomes redundant.
# We drop it to keep the schema clean and avoid double-indexing.


# ── Runner ────────────────────────────────────────────────────────────────────

def run():
    print("=" * 60)
    print("  add_event_id_constraint.py")
    print("  Adds UNIQUE constraint on movements.event_id")
    print("=" * 60)

    conn = get_connection()
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()

    # 1. Check if the constraint already exists ─────────────────────────────
    print("\n[1/4] Checking if constraint already exists...")
    cur.execute(CHECK_CONSTRAINT_EXISTS)
    if cur.fetchone():
        print("  ✓  Constraint 'movements_event_id_key' already exists.")
        print("     Nothing to do. Your DB is already set up correctly.")
        cur.close()
        conn.close()
        sys.exit(0)
    else:
        print("  →  Constraint not found. Will add it.")

    # 2. Check for duplicate event_id values in existing data ───────────────
    print("\n[2/4] Scanning for duplicate event_id values in existing data...")
    cur.execute(CHECK_DUPLICATES)
    dupes = cur.fetchall()
    if dupes:
        print("  ✗  DUPLICATE event_id values found — cannot add UNIQUE constraint.")
        print("     The following event_ids appear more than once:\n")
        print(f"     {'event_id':<40} {'count':>6}")
        print(f"     {'-'*40} {'-'*6}")
        for event_id, cnt in dupes:
            print(f"     {str(event_id):<40} {cnt:>6}")
        print(
            "\n     ACTION REQUIRED:\n"
            "     These are historical rows from the Excel migration that\n"
            "     happened to share an event_id (e.g. blank or placeholder).\n"
            "     Options:\n"
            "       A) UPDATE movements SET event_id = gen_random_uuid()::text\n"
            "             WHERE event_id IN (<duplicates above>);\n"
            "       B) Or contact your DBA to review and deduplicate.\n"
            "     Then re-run this script."
        )
        cur.close()
        conn.close()
        sys.exit(1)
    else:
        print(f"  ✓  No duplicates found. All existing event_id values are unique.")

    # 3. Add the UNIQUE constraint ───────────────────────────────────────────
    print("\n[3/4] Adding UNIQUE constraint on movements.event_id...")
    try:
        cur.execute(ADD_CONSTRAINT)
        print("  ✓  Constraint 'movements_event_id_key' added successfully.")
        print("     Existing rows were NOT modified.")
        print("     New movements with duplicate event_id will now be silently")
        print("     ignored (ON CONFLICT DO NOTHING), providing idempotency.")
    except psycopg2.Error as exc:
        print(f"  ✗  Failed to add constraint: {exc}")
        cur.close()
        conn.close()
        sys.exit(1)

    # 4. Drop the now-redundant plain index ─────────────────────────────────
    print("\n[4/4] Dropping redundant plain index idx_movements_event_id...")
    try:
        cur.execute(DROP_OLD_INDEX)
        print("  ✓  idx_movements_event_id dropped (UNIQUE constraint creates")
        print("     its own index automatically — no duplicate indexes).")
    except psycopg2.Error as exc:
        # Non-fatal — the plain index not existing is fine.
        print(f"  ~  Could not drop old index (may not exist): {exc}")

    print("\n" + "=" * 60)
    print("  DONE — movements.event_id is now UNIQUE.")
    print("  All existing data is intact.")
    print("  ON CONFLICT (event_id) DO NOTHING is fully active.")
    print("=" * 60)

    cur.close()
    conn.close()


if __name__ == "__main__":
    run()
