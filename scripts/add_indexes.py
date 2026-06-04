"""
scripts/add_indexes.py
======================
Creates performance indexes on the movements table.

Run on Heroku:
    heroku run python scripts/add_indexes.py --app <your-app-name>

Each index is created with CONCURRENTLY so the table stays fully readable
and writable during the build — no downtime, no locking.

Safe to run multiple times — all statements use IF NOT EXISTS.
"""

import os
import sys
import time

import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# Indexes to create
# Each tuple: (index_name, description, SQL)
# ---------------------------------------------------------------------------

INDEXES = [
    (
        "idx_movements_type_part_ts",
        "movement_type + indus_part + timestamp — covers stock/aggregation queries",
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_movements_type_part_ts
        ON movements (movement_type, indus_part, timestamp_utc DESC)
        """,
    ),
    (
        "idx_movements_hu_ts",
        "handling_unit + timestamp + id — covers DISTINCT ON for pallet-statuses",
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_movements_hu_ts
        ON movements (handling_unit, timestamp_utc DESC, id DESC)
        """,
    ),
    (
        "idx_movements_ts_desc",
        "timestamp DESC + id — covers order-by on movements tab",
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_movements_ts_desc
        ON movements (timestamp_utc DESC, id DESC)
        """,
    ),
    (
        "idx_movements_location",
        "location + movement_type — covers location filter queries",
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_movements_location
        ON movements (location, movement_type)
        """,
    ),
]

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_conn():
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("ERROR: DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]
    return psycopg2.connect(dsn=db_url, cursor_factory=psycopg2.extras.RealDictCursor)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_existing_indexes(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT indexname
            FROM pg_indexes
            WHERE tablename = 'movements'
        """)
        return {row["indexname"] for row in cur.fetchall()}


def get_row_count(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM movements")
        row = cur.fetchone()
        return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  indus-warehouse-tracker — movements index migration")
    print("=" * 60)

    conn = get_conn()
    # CONCURRENTLY requires autocommit — cannot run inside a transaction block
    conn.autocommit = True

    try:
        row_count = get_row_count(conn)
        print(f"\n  movements table: {row_count:,} rows\n")

        existing = get_existing_indexes(conn)
        print(f"  Existing indexes on movements: {len(existing)}")
        for idx in sorted(existing):
            print(f"    - {idx}")
        print()

        results = []

        for name, description, sql in INDEXES:
            if name in existing:
                print(f"  [SKIP]  {name}")
                print(f"          Already exists — nothing to do.\n")
                results.append((name, "skipped"))
                continue

            print(f"  [BUILD] {name}")
            print(f"          {description}")
            print(f"          Building... (table stays online during this)")

            t0 = time.time()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql.strip())
                elapsed = time.time() - t0
                print(f"          Done in {elapsed:.1f}s\n")
                results.append((name, f"created ({elapsed:.1f}s)"))
            except Exception as exc:
                print(f"          ERROR: {exc}\n", file=sys.stderr)
                results.append((name, f"FAILED: {exc}"))

        print("=" * 60)
        print("  Summary")
        print("=" * 60)
        for name, status in results:
            print(f"  {name:<45}  {status}")

        print()
        failures = [n for n, s in results if s.startswith("FAILED")]
        if failures:
            print(f"  {len(failures)} index(es) failed — check errors above.")
            sys.exit(1)
        else:
            print("  All indexes are in place. Dashboard should be faster now.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
