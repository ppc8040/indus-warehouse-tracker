"""
Add auditable, idempotent warehouse loading tables.

Run:
    python scripts/add_loading_schema.py

This migration is additive. It does not alter existing movement behaviour.
"""

import os
import sys
from urllib.parse import urlparse

import psycopg2


def get_database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is required.")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def main():
    database_url = get_database_url()
    parsed = urlparse(database_url)

    if not parsed.hostname:
        raise RuntimeError("DATABASE_URL is invalid.")

    print(f"Target database host: {parsed.hostname}")
    print("Creating loading schema...")

    statements = [
        """
        CREATE TABLE IF NOT EXISTS loading_headers (
            loading_id UUID PRIMARY KEY,
            idempotency_key VARCHAR(120) NOT NULL UNIQUE,
            status VARCHAR(20) NOT NULL
                CHECK (status IN ('POSTING', 'COMPLETED', 'FAILED')),
            source_location VARCHAR(20) NOT NULL,
            destination VARCHAR(20) NOT NULL DEFAULT 'LOADING',
            operator_id VARCHAR(100) NOT NULL,
            device_id VARCHAR(200) NOT NULL DEFAULT '',
            supervisor_override BOOLEAN NOT NULL DEFAULT FALSE,
            override_reason VARCHAR(500) NOT NULL DEFAULT '',
            item_count INTEGER NOT NULL CHECK (item_count > 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS loading_items (
            loading_item_id UUID PRIMARY KEY,
            loading_id UUID NOT NULL
                REFERENCES loading_headers(loading_id) ON DELETE RESTRICT,
            handling_unit VARCHAR(20) NOT NULL,
            sequence_no INTEGER NOT NULL,
            resolution VARCHAR(30) NOT NULL
                CHECK (resolution IN ('EXISTING_STOCK_IN', 'MISSING_HU_FALLBACK')),
            stock_in_event_id VARCHAR(12),
            stock_out_event_id VARCHAR(12) NOT NULL,
            indus_part VARCHAR(7) NOT NULL,
            customer_part VARCHAR(200) NOT NULL DEFAULT '',
            pallet_qty INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (loading_id, handling_unit),
            UNIQUE (loading_id, sequence_no)
        )
        """,
        """
        ALTER TABLE movements
            ADD COLUMN IF NOT EXISTS loading_id UUID
        """,
        """
        ALTER TABLE movements
            ADD COLUMN IF NOT EXISTS loading_item_id UUID
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_loading_headers_created_at
            ON loading_headers (created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_loading_headers_status_created_at
            ON loading_headers (status, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_loading_items_loading_id
            ON loading_items (loading_id, sequence_no)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_loading_items_handling_unit
            ON loading_items (handling_unit, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_movements_loading_id
            ON movements (loading_id)
            WHERE loading_id IS NOT NULL
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_movements_loading_item_id
            ON movements (loading_item_id)
            WHERE loading_item_id IS NOT NULL
        """,
    ]

    connection = psycopg2.connect(database_url)
    try:
        connection.autocommit = False
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
        connection.commit()
        print("Loading schema created successfully.")
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
