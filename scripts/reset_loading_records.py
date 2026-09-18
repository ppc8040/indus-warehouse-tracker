# scripts/reset_loading_records.py
"""
Delete loading audit records only.

This script deletes data from:
  1. loading_items
  2. loading_headers

It does NOT modify:
  - movements
  - pallet_status
  - ivt_scans
  - bdr_scans
  - any database schema

Run once from the Heroku console:

    heroku run python scripts/reset_loading_records.py -a YOUR_HEROKU_APP

The deletion runs in one database transaction. If any step fails, PostgreSQL
rolls the entire operation back and no partial reset is committed.
"""

from app.services.db_client import db_transaction


def _count_rows(cursor, table_name):
    cursor.execute(f"SELECT COUNT(*) AS count FROM {table_name}")
    return int(cursor.fetchone()["count"])


def main():
    with db_transaction() as conn:
        with conn.cursor() as cur:
            loading_items_before = _count_rows(cur, "loading_items")
            loading_headers_before = _count_rows(cur, "loading_headers")

            print("Loading-data reset preview")
            print(f"  loading_items:   {loading_items_before}")
            print(f"  loading_headers: {loading_headers_before}")
            print("  movements:       NOT TOUCHED")
            print("  pallet_status:   NOT TOUCHED")
            print()

            cur.execute("DELETE FROM loading_items")
            deleted_items = cur.rowcount

            cur.execute("DELETE FROM loading_headers")
            deleted_headers = cur.rowcount

            loading_items_after = _count_rows(cur, "loading_items")
            loading_headers_after = _count_rows(cur, "loading_headers")

            if loading_items_after != 0 or loading_headers_after != 0:
                raise RuntimeError(
                    "Loading reset verification failed: "
                    "one or more loading tables still contain records."
                )

            print("Loading-data reset completed successfully.")
            print(f"  Deleted loading_items:   {deleted_items}")
            print(f"  Deleted loading_headers: {deleted_headers}")
            print("  Movement and pallet-status data were not changed.")


if __name__ == "__main__":
    main()
