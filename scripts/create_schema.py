"""
scripts/create_schema.py

One-time Postgres schema creation script.
Run ONCE before migration and before any app code changes go live.

Usage:
    heroku run python scripts/create_schema.py --app indus-warehouse-tracker
    OR locally with DATABASE_URL set in your .env

Safe to re-run — all statements use IF NOT EXISTS / CREATE OR REPLACE.
"""

import os
import sys
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

# ── Connection ───────────────────────────────────────────────────────────────────

def get_connection():
    uri = os.environ.get("DATABASE_URL")
    if not uri:
        print("ERROR: DATABASE_URL environment variable is not set.")
        sys.exit(1)
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(uri)


# ── DDL Statements ─────────────────────────────────────────────────────────────

CREATE_MOVEMENTS = """
CREATE TABLE IF NOT EXISTS movements (
    id                BIGSERIAL       PRIMARY KEY,
    event_id          TEXT            NOT NULL,
    timestamp_utc     TIMESTAMPTZ     NOT NULL,
    handling_unit     TEXT            NOT NULL,
    indus_part        TEXT,
    customer_part     TEXT,
    pallet_qty        INTEGER,
    job_no            TEXT,
    movement_type     TEXT            NOT NULL CHECK (movement_type IN ('STOCK_IN', 'STOCK_OUT')),
    operator_id       TEXT,
    device_id         TEXT,
    sync_status       TEXT            NOT NULL DEFAULT 'SYNCED' CHECK (sync_status IN ('SYNCED', 'PENDING')),
    validation_status TEXT            NOT NULL DEFAULT 'VALID'  CHECK (validation_status IN ('VALID', 'OVERRIDE')),
    location          TEXT,
    destination       TEXT
);
"""

CREATE_MOVEMENTS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_movements_event_id    ON movements (event_id);
CREATE INDEX IF NOT EXISTS idx_movements_handling_unit ON movements (handling_unit);
CREATE INDEX IF NOT EXISTS idx_movements_timestamp     ON movements (timestamp_utc DESC);
CREATE INDEX IF NOT EXISTS idx_movements_movement_type ON movements (movement_type);
CREATE INDEX IF NOT EXISTS idx_movements_location      ON movements (location);
"""

CREATE_PALLET_STATUS = """
CREATE TABLE IF NOT EXISTS pallet_status (
    handling_unit       TEXT        PRIMARY KEY,
    current_status      TEXT        NOT NULL CHECK (current_status IN ('IN_WAREHOUSE', 'OUT_OF_WAREHOUSE')),
    last_event_id       TEXT,
    last_movement_type  TEXT        NOT NULL CHECK (last_movement_type IN ('STOCK_IN', 'STOCK_OUT')),
    last_movement_time  TIMESTAMPTZ NOT NULL,
    indus_part          TEXT,
    pallet_qty          INTEGER,
    last_location       TEXT
);
"""

CREATE_IVT_SCANS = """
CREATE TABLE IF NOT EXISTS ivt_scans (
    id           BIGSERIAL   PRIMARY KEY,
    code         TEXT        NOT NULL UNIQUE,
    scanned_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    location     TEXT
);

CREATE INDEX IF NOT EXISTS idx_ivt_scans_code ON ivt_scans (code);
"""

CREATE_ERROR_LOG = """
CREATE TABLE IF NOT EXISTS error_log (
    id            BIGSERIAL   PRIMARY KEY,
    error_id      TEXT        NOT NULL UNIQUE,
    timestamp_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    handling_unit TEXT,
    stage         TEXT        CHECK (stage IN ('LOOKUP', 'MOVEMENT', 'SYNC', 'OVERRIDE', 'SYSTEM')),
    error_code    TEXT,
    error_message TEXT,
    resolved_flag TEXT        NOT NULL DEFAULT 'NO' CHECK (resolved_flag IN ('NO', 'YES'))
);
"""

# ── SQL Views (replace all Excel formula/pivot sheets) ────────────────────────

CREATE_VIEW_STOCK_SUMMARY = """
CREATE OR REPLACE VIEW v_stock_summary AS
SELECT
    ps.handling_unit,
    ps.current_status,
    ps.last_movement_type,
    ps.last_movement_time,
    ps.indus_part,
    ps.pallet_qty,
    ps.last_location,
    ps.last_event_id
FROM pallet_status ps
ORDER BY ps.last_movement_time DESC;
"""

CREATE_VIEW_STOCK_BY_LOCATION = """
CREATE OR REPLACE VIEW v_stock_by_location AS
SELECT
    ps.last_location          AS location,
    ps.indus_part,
    COUNT(*)                  AS pallet_count,
    SUM(ps.pallet_qty)        AS total_qty
FROM pallet_status ps
WHERE ps.current_status = 'IN_WAREHOUSE'
GROUP BY ps.last_location, ps.indus_part
ORDER BY ps.last_location, ps.indus_part;
"""

CREATE_VIEW_MOVEMENT_VELOCITY = """
CREATE OR REPLACE VIEW v_movement_velocity AS
SELECT
    DATE(timestamp_utc AT TIME ZONE 'Asia/Dubai') AS movement_date,
    movement_type,
    COUNT(*)                                       AS movement_count
FROM movements
GROUP BY DATE(timestamp_utc AT TIME ZONE 'Asia/Dubai'), movement_type
ORDER BY movement_date DESC, movement_type;
"""

CREATE_VIEW_OPERATOR_ACTIVITY = """
CREATE OR REPLACE VIEW v_operator_activity AS
SELECT
    operator_id,
    movement_type,
    COUNT(*)        AS total_movements,
    MAX(timestamp_utc AT TIME ZONE 'Asia/Dubai') AS last_active
FROM movements
WHERE operator_id IS NOT NULL
GROUP BY operator_id, movement_type
ORDER BY last_active DESC;
"""


# ── Runner ───────────────────────────────────────────────────────────────────

def run():
    print("Connecting to Postgres...")
    conn = get_connection()
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()

    steps = [
        ("movements table",              CREATE_MOVEMENTS),
        ("movements indexes",             CREATE_MOVEMENTS_INDEXES),
        ("pallet_status table",           CREATE_PALLET_STATUS),
        ("ivt_scans table",               CREATE_IVT_SCANS),
        ("error_log table",               CREATE_ERROR_LOG),
        ("v_stock_summary view",          CREATE_VIEW_STOCK_SUMMARY),
        ("v_stock_by_location view",      CREATE_VIEW_STOCK_BY_LOCATION),
        ("v_movement_velocity view",      CREATE_VIEW_MOVEMENT_VELOCITY),
        ("v_operator_activity view",      CREATE_VIEW_OPERATOR_ACTIVITY),
    ]

    all_ok = True
    for label, sql in steps:
        try:
            cur.execute(sql)
            print(f"  \u2713  {label}")
        except Exception as e:
            print(f"  \u2717  {label} FAILED: {e}")
            all_ok = False

    cur.close()
    conn.close()

    if all_ok:
        print("\nSchema creation complete. All tables and views are ready.")
        print("Next step: run scripts/migrate_excel_to_pg.py")
    else:
        print("\nOne or more steps failed. Fix errors above before running the migration.")
        sys.exit(1)


if __name__ == "__main__":
    run()
