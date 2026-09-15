"""
Add an optional shipment/container reference to each loading header.

Run once:
    python scripts/add_loading_shipment_reference.py
"""

import os
import sys

import psycopg2


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()

    if not url:
        raise RuntimeError("DATABASE_URL is required.")

    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]

    return url


def main() -> None:
    connection = psycopg2.connect(get_database_url())

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                ALTER TABLE loading_headers
                ADD COLUMN IF NOT EXISTS shipment_reference VARCHAR(200)
                NOT NULL DEFAULT ''
                """
            )

            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_loading_headers_shipment_reference
                ON loading_headers (shipment_reference)
                WHERE shipment_reference <> ''
                """
            )

        connection.commit()
        print("Loading shipment reference column is ready.")

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        sys.exit(1)
