"""
One-time migration: adds hu_creation_date and hu_batch columns to
pallet_status. Safe to run multiple times (IF NOT EXISTS).

Usage:
    heroku run python scripts/add_hu_metadata_columns.py --app <your-app-name>
"""
import os
import sys
import psycopg2


def main():
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("ERROR: DATABASE_URL not set.")
        sys.exit(1)
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]

    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE pallet_status
                ADD COLUMN IF NOT EXISTS hu_creation_date TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS hu_batch TEXT;
            """)
            print("Done: hu_creation_date and hu_batch columns added to pallet_status.")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
