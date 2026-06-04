"""
scripts/migrate_pallet_qty_to_int.py
=====================================
Converts the pallet_qty column in the movements table from TEXT to INTEGER.

Run on Heroku:
    heroku run python scripts/migrate_pallet_qty_to_int.py --app indus-warehouse-inventory

What it does:
  1. Shows a preview of any non-numeric pallet_qty values BEFORE touching anything
  2. Asks for confirmation before proceeding
  3. Alters the column type using a safe COALESCE/REGEXP expression
     (blank strings, NULLs, and non-numeric values all safely become 0)
  4. Reports the result

This is safe to run once. Running it again after the column is already
INTEGER will detect the existing type and exit cleanly.
"""

import os
import sys

import psycopg2
import psycopg2.extras


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

def get_column_type(conn):
    """Return the current data_type of pallet_qty in movements."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT data_type
            FROM information_schema.columns
            WHERE table_name = 'movements'
              AND column_name = 'pallet_qty'
        """)
        row = cur.fetchone()
        return row["data_type"] if row else None


def get_non_numeric_preview(conn):
    """Return up to 20 rows where pallet_qty contains non-numeric characters."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, handling_unit, pallet_qty
            FROM movements
            WHERE pallet_qty IS NOT NULL
              AND pallet_qty::text !~ '^[0-9]*$'
            LIMIT 20
        """)
        return cur.fetchall()


def get_null_or_blank_count(conn):
    """Count rows where pallet_qty is NULL or empty string."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) AS cnt
            FROM movements
            WHERE pallet_qty IS NULL OR TRIM(pallet_qty::text) = ''
        """)
        row = cur.fetchone()
        return row["cnt"] if row else 0


def get_total_count(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM movements")
        row = cur.fetchone()
        return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  indus-warehouse-tracker — pallet_qty TEXT -> INTEGER")
    print("=" * 60)

    conn = get_conn()

    try:
        # --- Check current column type ---
        current_type = get_column_type(conn)
        print(f"\n  Current pallet_qty column type: {current_type}")

        if current_type and "int" in current_type.lower():
            print("  Column is already INTEGER. Nothing to do.")
            print("\n  Exiting cleanly.")
            return

        total = get_total_count(conn)
        print(f"  Total rows in movements: {total:,}")

        # --- Preview non-numeric values ---
        bad_rows = get_non_numeric_preview(conn)
        null_blank = get_null_or_blank_count(conn)

        print(f"\n  NULL or blank pallet_qty rows: {null_blank:,}")
        print(f"  (These will safely become 0 after migration)")

        if bad_rows:
            print(f"\n  Non-numeric pallet_qty values found (sample):")
            print(f"  {'id':<8} {'handling_unit':<20} {'pallet_qty':<20}")
            print(f"  {'-'*8} {'-'*20} {'-'*20}")
            for row in bad_rows:
                print(f"  {str(row['id']):<8} {str(row['handling_unit']):<20} {str(row['pallet_qty']):<20}")
            print(f"\n  These will be converted: digits extracted, rest stripped.")
            print(f"  e.g. '12 pcs' -> 12, 'N/A' -> 0, '' -> 0")
        else:
            print("\n  No non-numeric values found. Clean migration.")

        # --- Confirm ---
        print("\n" + "-" * 60)
        confirm = input("  Proceed with ALTER TABLE? Type YES to continue: ").strip()
        if confirm != "YES":
            print("  Aborted. No changes made.")
            return

        # --- Run migration ---
        print("\n  Running ALTER TABLE...")
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE movements
                ALTER COLUMN pallet_qty TYPE INTEGER
                USING COALESCE(
                    NULLIF(REGEXP_REPLACE(pallet_qty::text, '[^0-9]', '', 'g'), ''),
                    '0'
                )::integer
            """)
        conn.commit()

        # --- Verify ---
        new_type = get_column_type(conn)
        print(f"  Done. pallet_qty column type is now: {new_type}")
        print()
        print("  Next step: update admin.py STOCK_QTY_EXPR to:")
        print('    STOCK_QTY_EXPR = "COALESCE(pallet_qty, 0)"')
        print()
        print("=" * 60)
        print("  Migration complete.")
        print("=" * 60)

    except Exception as exc:
        conn.rollback()
        print(f"\n  ERROR: {exc}", file=sys.stderr)
        print("  No changes were committed.", file=sys.stderr)
        sys.exit(1)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
