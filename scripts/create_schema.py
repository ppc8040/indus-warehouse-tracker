"""
Create the Postgres schema for the Indus Warehouse Tracker.

Run once against the Heroku Postgres DB:
  heroku run python scripts/create_schema.py --app <your-app-name>

All CREATE TABLE statements are guarded by IF NOT EXISTS so this script
is safe to re-run — it will not destroy existing data.
"""
import os
import psycopg2

DDL = """
CREATE TABLE IF NOT EXISTS movements (
    id                SERIAL PRIMARY KEY,
    event_id          TEXT UNIQUE NOT NULL,
    timestamp_utc     TIMESTAMPTZ NOT NULL,
    handling_unit     TEXT NOT NULL,
    indus_part        TEXT,
    customer_part     TEXT,
    pallet_qty        TEXT,
    job_no            TEXT,
    movement_type     TEXT NOT NULL,
    operator_id       TEXT,
    device_id         TEXT,
    sync_status       TEXT,
    validation_status TEXT,
    location          TEXT,
    from_location     TEXT,
    to_location       TEXT
);

CREATE TABLE IF NOT EXISTS pallet_status (
    handling_unit      TEXT PRIMARY KEY,
    current_status     TEXT,
    last_event_id      TEXT,
    last_movement_type TEXT,
    last_movement_time TIMESTAMPTZ,
    indus_part         TEXT,
    pallet_qty         TEXT,
    last_location      TEXT
);

CREATE TABLE IF NOT EXISTS ivt_scans (
    id           SERIAL PRIMARY KEY,
    code         TEXT UNIQUE NOT NULL,
    scanned_date TIMESTAMPTZ NOT NULL,
    location     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bdr_scans (
    id            SERIAL PRIMARY KEY,
    code          TEXT UNIQUE NOT NULL,
    customer_code TEXT NOT NULL,
    part_number   TEXT NOT NULL,
    year_2d       TEXT NOT NULL,
    doy           TEXT NOT NULL,
    serial        TEXT NOT NULL,
    scanned_date  TIMESTAMPTZ NOT NULL,
    location      TEXT NOT NULL
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_movements_hu         ON movements (handling_unit);
CREATE INDEX IF NOT EXISTS idx_movements_ts         ON movements (timestamp_utc DESC);
CREATE INDEX IF NOT EXISTS idx_movements_type       ON movements (movement_type);
CREATE INDEX IF NOT EXISTS idx_movements_location   ON movements (location);
CREATE INDEX IF NOT EXISTS idx_ivt_scans_date       ON ivt_scans (scanned_date DESC);
CREATE INDEX IF NOT EXISTS idx_bdr_scans_part       ON bdr_scans (part_number);
CREATE INDEX IF NOT EXISTS idx_bdr_scans_serial     ON bdr_scans (serial, part_number);
CREATE INDEX IF NOT EXISTS idx_bdr_scans_date       ON bdr_scans (scanned_date DESC);
CREATE INDEX IF NOT EXISTS idx_bdr_scans_location   ON bdr_scans (location);
"""


def main():
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise SystemExit("ERROR: DATABASE_URL environment variable is not set.")
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]

    conn = psycopg2.connect(db_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(DDL)
        print("Schema created / verified OK.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
