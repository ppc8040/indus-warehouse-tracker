"""
scripts/fix_duplicate_event_ids.py

Reassigns a fresh uuid4-based event_id ONLY to rows that share an
event_id with one or more other rows (historical Excel migration artefacts).

Safety contract (verified in code below):
  - Only the event_id column is written to.
  - Every other column (handling_unit, movement_type, indus_part,
    pallet_qty, timestamp_utc, location, operator_id, etc.) is
    completely untouched.
  - For each duplicate group, the FIRST row (lowest id) keeps its
    original event_id so at least one row preserves the historical
    identifier. Only the duplicates (2nd, 3rd, … occurrence) are
    given new IDs.
  - pallet_status.last_event_id is updated wherever it referenced
    one of the renamed event_ids, so the FK relationship stays
    consistent.
  - The script is idempotent: re-running it after success is a no-op
    (no more duplicates → nothing to change).
  - Everything runs inside a single transaction; any error triggers a
    full rollback so the database is never left in a partial state.

Run AFTER this script: scripts/add_event_id_constraint.py
  That script will add the UNIQUE constraint now that all event_ids
  are distinct.

Usage:
    heroku run python scripts/fix_duplicate_event_ids.py --app indus-warehouse-tracker
    OR locally with DATABASE_URL set in your .env
"""

import os
import sys
import uuid

import psycopg2
import psycopg2.extras


# ── Connection ────────────────────────────────────────────────────────────────

def get_connection():
    uri = os.environ.get("DATABASE_URL", "")
    if not uri:
        print("ERROR: DATABASE_URL environment variable is not set.")
        sys.exit(1)
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    try:
        return psycopg2.connect(uri, cursor_factory=psycopg2.extras.RealDictCursor)
    except psycopg2.Error as exc:
        print(f"ERROR: Could not connect to Postgres: {exc}")
        sys.exit(1)


# ── SQL ──────────────────────────────────────────────────────────────────────

# Find every row that is NOT the first occurrence of its event_id.
# "First" = lowest `id` (insertion order from the migration).
# We return: the row's `id` (PK), its current event_id, and its
# handling_unit so we can print a useful audit log.
FIND_DUPLICATES_TO_FIX = """
    SELECT m.id, m.event_id, m.handling_unit, m.movement_type, m.timestamp_utc
    FROM   movements m
    INNER JOIN (
        -- For each duplicate event_id, find the minimum id (the "keeper")
        SELECT event_id, MIN(id) AS keep_id
        FROM   movements
        GROUP  BY event_id
        HAVING COUNT(*) > 1
    ) dups ON m.event_id = dups.event_id
          AND m.id       <> dups.keep_id
    ORDER  BY m.event_id, m.id;
"""

# Only update the event_id column on the specific row by primary key.
# Nothing else is touched.
UPDATE_EVENT_ID = """
    UPDATE movements
    SET    event_id = %(new_id)s
    WHERE  id       = %(row_id)s;
"""

# If pallet_status.last_event_id pointed at one of the old duplicate
# event_ids that we just renamed, update it to the new id.
FIX_PALLET_STATUS_FK = """
    UPDATE pallet_status
    SET    last_event_id = %(new_id)s
    WHERE  last_event_id = %(old_id)s;
"""

# Verify no duplicates remain after the update.
VERIFY_NO_DUPLICATES = """
    SELECT event_id, COUNT(*) AS cnt
    FROM   movements
    GROUP  BY event_id
    HAVING COUNT(*) > 1;
"""


# ── Runner ────────────────────────────────────────────────────────────────────

def run():
    print("=" * 65)
    print("  fix_duplicate_event_ids.py")
    print("  Reassigns event_id ONLY on duplicate historical rows")
    print("=" * 65)

    conn = get_connection()

    # Everything runs in ONE transaction so any error does a full rollback.
    conn.autocommit = False

    try:
        cur = conn.cursor()

        # 1. Find duplicate rows to fix ─────────────────────────────────
        print("\n[1/4] Finding duplicate event_id rows to fix...")
        cur.execute(FIND_DUPLICATES_TO_FIX)
        rows_to_fix = cur.fetchall()

        if not rows_to_fix:
            print("  ✓  No duplicate event_id rows found.")
            print("     Nothing to do. Run add_event_id_constraint.py next.")
            conn.rollback()
            cur.close()
            conn.close()
            sys.exit(0)

        print(f"  →  Found {len(rows_to_fix)} row(s) that need a new event_id.")
        print(f"     (First occurrence of each duplicate keeps the original id.)")

        # 2. Print audit table before making any changes ───────────────
        print("\n[2/4] Audit log of rows that will be re-keyed:")
        print(f"  {'ROW_ID':<10} {'OLD_EVENT_ID':<25} {'HU':<22} {'TYPE':<12} TIMESTAMP")
        print(f"  {'-'*10} {'-'*25} {'-'*22} {'-'*12} {'-'*25}")
        for r in rows_to_fix:
            ts = str(r['timestamp_utc'])[:19] if r['timestamp_utc'] else 'N/A'
            print(
                f"  {str(r['id']):<10} "
                f"{str(r['event_id']):<25} "
                f"{str(r['handling_unit'] or ''):<22} "
                f"{str(r['movement_type'] or ''):<12} "
                f"{ts}"
            )

        # 3. Apply updates inside the open transaction ────────────────
        print(f"\n[3/4] Updating event_id on {len(rows_to_fix)} row(s)...")
        print(f"  {'ROW_ID':<10} {'OLD_EVENT_ID':<25} → NEW_EVENT_ID")
        print(f"  {'-'*10} {'-'*25}   {'-'*25}")

        id_remap = {}  # old_event_id → new_event_id (for pallet_status update)

        for r in rows_to_fix:
            old_id  = r['event_id']
            # Generate a fresh ID: prefix MIGFIX- so it's visually distinct
            # in admin logs from live-scan IDs which use the plain uuid4 format.
            new_id  = 'MIGFIX-' + str(uuid.uuid4()).upper()[:12]

            cur.execute(UPDATE_EVENT_ID, {'new_id': new_id, 'row_id': r['id']})

            # Track for pallet_status FK repair
            id_remap[old_id] = new_id

            print(f"  {str(r['id']):<10} {str(old_id):<25} → {new_id}")

        # 4. Repair pallet_status.last_event_id where it pointed to a renamed id
        print("\n[4/4] Checking pallet_status.last_event_id for references to renamed IDs...")
        repaired = 0
        for old_id, new_id in id_remap.items():
            cur.execute(FIX_PALLET_STATUS_FK, {'new_id': new_id, 'old_id': old_id})
            if cur.rowcount > 0:
                print(f"  ✓  pallet_status updated: {old_id} → {new_id}")
                repaired += cur.rowcount
        if repaired == 0:
            print("  ✓  No pallet_status rows needed updating.")

        # 5. Verify: run duplicate check again inside the transaction ─────
        print("\n[5/5] Verifying no duplicates remain...")
        cur.execute(VERIFY_NO_DUPLICATES)
        remaining = cur.fetchall()
        if remaining:
            print("  ✗  Duplicates still found after update — rolling back:")
            for row in remaining:
                print(f"       event_id={row['event_id']}  count={row['cnt']}")
            conn.rollback()
            print("  ✗  ROLLBACK complete. Database is unchanged.")
            cur.close()
            conn.close()
            sys.exit(1)

        print("  ✓  Zero duplicates remaining.")

        # 6. Commit ──────────────────────────────────────────────────────────
        conn.commit()
        print()
        print("=" * 65)
        print("  DONE — All duplicates resolved and committed.")
        print(f"  {len(rows_to_fix)} event_id(s) reassigned (only event_id column).")
        print(f"  {repaired} pallet_status row(s) updated to match.")
        print("  All other data (HU, parts, qty, timestamps) unchanged.")
        print()
        print("  NEXT STEP: run scripts/add_event_id_constraint.py")
        print("=" * 65)

    except Exception as exc:
        conn.rollback()
        print(f"\n  ✗  UNEXPECTED ERROR — full rollback applied: {exc}")
        print("     Database is unchanged.")
        cur.close()
        conn.close()
        sys.exit(1)

    cur.close()
    conn.close()


if __name__ == "__main__":
    run()
