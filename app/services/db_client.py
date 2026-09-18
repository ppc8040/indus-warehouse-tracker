"""
db_client.py — PostgreSQL data access layer.

Replaces graph_client.py as the primary data store interface.

Schema (created by scripts/create_schema.py):

  movements (
      id               SERIAL PRIMARY KEY,
      event_id         TEXT UNIQUE NOT NULL,
      timestamp_utc    TIMESTAMPTZ NOT NULL,
      handling_unit    TEXT NOT NULL,
      indus_part       TEXT,
      customer_part    TEXT,
      pallet_qty       TEXT,
      job_no           TEXT,
      movement_type    TEXT NOT NULL,
      operator_id      TEXT,
      device_id        TEXT,
      sync_status      TEXT,
      validation_status TEXT,
      location         TEXT,
      from_location    TEXT,
      to_location      TEXT
  )

  pallet_status (
      handling_unit       TEXT PRIMARY KEY,
      current_status      TEXT,
      last_event_id       TEXT,
      last_movement_type  TEXT,
      last_movement_time  TIMESTAMPTZ,
      indus_part          TEXT,
      pallet_qty          TEXT,
      last_location       TEXT,
      hu_creation_date    TIMESTAMPTZ,
      hu_batch            TEXT
  )

  ivt_scans (
      id           SERIAL PRIMARY KEY,
      code         TEXT UNIQUE NOT NULL,
      scanned_date TIMESTAMPTZ NOT NULL,
      location     TEXT NOT NULL
  )

  bdr_scans (
      id            SERIAL PRIMARY KEY,
      code          TEXT UNIQUE NOT NULL,
      customer_code TEXT NOT NULL,
      part_number   TEXT NOT NULL,
      year_2d       TEXT NOT NULL,
      doy           TEXT NOT NULL,
      serial        TEXT NOT NULL,
      scanned_date  TIMESTAMPTZ NOT NULL,
      location      TEXT NOT NULL
  )

Connection strategy:
  A module-level ThreadedConnectionPool (min=1, max=10) is created once
  on first use. Heroku Postgres DSN already contains sslmode=require in
  the URL — we do NOT pass sslmode as a separate kwarg to avoid a
  conflict that corrupts the connection.
  Every public function acquires a connection, uses it, and always
  returns it exactly once via a tracked flag in _get_conn().
"""

import atexit
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class DbError(Exception):
    """Raised for any database-level failure in this module."""
    def __init__(self, message, cause=None):
        super().__init__(message)
        self.message = message
        self.cause = cause

    def __str__(self):
        return self.message


# ---------------------------------------------------------------------------
# Connection pool (lazy-initialised)
# ---------------------------------------------------------------------------

_pool: pg_pool.ThreadedConnectionPool | None = None


def _get_pool() -> pg_pool.ThreadedConnectionPool:
    """Return the module-level connection pool, creating it on first call."""
    global _pool
    if _pool is None:
        db_url = os.environ.get("DATABASE_URL", "")
        if not db_url:
            raise DbError(
                "DATABASE_URL environment variable is not set. "
                "Add it to your Heroku config vars."
            )
        # Heroku Postgres URLs start with postgres:// but psycopg2
        # requires postgresql:// — fix silently.
        if db_url.startswith("postgres://"):
            db_url = "postgresql://" + db_url[len("postgres://"):]

        # NOTE: do NOT pass sslmode as a kwarg — Heroku's DSN already
        # contains sslmode=require in the query string. Passing it again
        # as a keyword argument causes a libpq conflict that breaks every
        # connection silently.
        try:
            _pool = pg_pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=db_url,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
            logger.info("PostgreSQL connection pool created (min=1 max=10).")
            atexit.register(_close_pool)
        except psycopg2.Error as exc:
            raise DbError(f"Failed to create connection pool: {exc}", cause=exc) from exc
    return _pool


def _close_pool():
    """Close all connections on dyno shutdown."""
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
            logger.info("PostgreSQL connection pool closed.")
        except Exception as exc:
            logger.warning("Error closing connection pool: %s", exc)
        _pool = None


@contextmanager
def _get_conn():
    """
    Borrow a connection from the pool, yield it, commit on clean exit.

    The connection is returned to the pool exactly once. A `_returned`
    flag prevents the double-putconn bug where an exception handler and
    the finally block both try to return the same connection, corrupting
    the pool and causing every subsequent request to fail.
    """
    p = _get_pool()
    conn = p.getconn()
    _returned = False
    try:
        yield conn
        conn.commit()
    except psycopg2.Error as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        raise DbError(f"Database error: {exc}", cause=exc) from exc
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if not _returned:
            _returned = True
            try:
                p.putconn(conn)
            except Exception as e:
                logger.warning("putconn failed: %s", e)


# ---------------------------------------------------------------------------
# movements table
# ---------------------------------------------------------------------------

def insert_movement(
    event_id: str,
    timestamp_utc: str,
    handling_unit: str,
    movement_type: str,
    indus_part: str = "",
    customer_part: str = "",
    pallet_qty: str = "",
    job_no: str = "",
    operator_id: str = "WORKER",
    device_id: str = "",
    sync_status: str = "SYNCED",
    validation_status: str = "VALID",
    location: str = "",
    from_location: str = "",
    to_location: str = "",
    loading_id: str = "",
    loading_item_id: str = "",
) -> None:
    """
    Append one row to the movements table.
    ON CONFLICT DO NOTHING ensures idempotency for offline-sync replays.
    """
    sql = """
        INSERT INTO movements (
            event_id, timestamp_utc, handling_unit, indus_part,
            customer_part, pallet_qty, job_no, movement_type,
            operator_id, device_id, sync_status, validation_status,
            location, from_location, to_location, loading_id, loading_item_id
        ) VALUES (
            %(event_id)s, %(timestamp_utc)s, %(handling_unit)s, %(indus_part)s,
            %(customer_part)s, %(pallet_qty)s, %(job_no)s, %(movement_type)s,
            %(operator_id)s, %(device_id)s, %(sync_status)s, %(validation_status)s,
            %(location)s, %(from_location)s, %(to_location)s, %(loading_id)s, %(loading_item_id)s
        )
        ON CONFLICT (event_id) DO NOTHING
    """
    params = dict(
        event_id=event_id,
        timestamp_utc=_parse_ts(timestamp_utc),
        handling_unit=handling_unit,
        indus_part=indus_part or "",
        customer_part=customer_part or "",
        pallet_qty=pallet_qty or "",
        job_no=job_no or "",
        movement_type=movement_type,
        operator_id=operator_id or "WORKER",
        device_id=device_id or "",
        sync_status=sync_status or "SYNCED",
        validation_status=validation_status or "VALID",
        location=location or "",
        from_location=from_location or "",
        to_location=to_location or "",
        loading_id=loading_id or None,
        loading_item_id=loading_item_id or None,
    )
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
    logger.debug("insert_movement: event_id=%s hu=%s type=%s", event_id, handling_unit, movement_type)


def get_recent_movements(limit: int = 50) -> list[dict]:
    """Return the most recent `limit` rows from movements, newest first."""
    sql = """
        SELECT
            id, event_id,
            timestamp_utc AT TIME ZONE 'Asia/Dubai' AS timestamp_dubai,
            handling_unit, indus_part, customer_part, pallet_qty,
            job_no, movement_type, operator_id, device_id,
            sync_status, validation_status, location,
            from_location, to_location
        FROM movements
        ORDER BY timestamp_utc DESC, id DESC
        LIMIT %(limit)s
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"limit": limit})
            rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def get_last_movement_for_hu(handling_unit: str) -> dict | None:
    """
    Return the most recent movement row for a given HU, or None.

    Secondary sort on id DESC breaks ties when two events share the same
    timestamp_utc (e.g. IN and OUT scanned within the same second).
    The higher id was inserted later and reflects the true current state.
    """
    sql = """
        SELECT movement_type, validation_status, location, timestamp_utc
        FROM movements
        WHERE handling_unit = %(hu)s
        ORDER BY timestamp_utc DESC, id DESC
        LIMIT 1
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"hu": handling_unit})
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def get_all_movements_for_hu(handling_unit: str) -> list[dict]:
    """Return all movement rows for a HU, oldest first."""
    sql = """
        SELECT
            id, event_id,
            timestamp_utc AT TIME ZONE 'Asia/Dubai' AS timestamp_dubai,
            handling_unit, indus_part, customer_part, pallet_qty,
            job_no, movement_type, operator_id, device_id,
            sync_status, validation_status, location,
            from_location, to_location
        FROM movements
        WHERE handling_unit = %(hu)s
        ORDER BY timestamp_utc ASC, id ASC
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"hu": handling_unit})
            rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# pallet_status table
# ---------------------------------------------------------------------------

def get_pallet_status(handling_unit: str) -> dict | None:
    """Return current status for a single HU, including creation date and batch
    from pallet_status (write-once fields). Used by movement_service to detect
    whether this is a first-time scan."""
    sql = """
        SELECT DISTINCT ON (m.handling_unit)
            m.handling_unit,
            CASE WHEN m.movement_type = 'STOCK_IN' THEN 'IN_WAREHOUSE'
                 ELSE 'OUT_OF_WAREHOUSE' END AS current_status,
            m.event_id          AS last_event_id,
            m.movement_type     AS last_movement_type,
            m.timestamp_utc AT TIME ZONE 'Asia/Dubai' AS last_movement_time_dubai,
            m.indus_part,
            m.pallet_qty,
            m.location          AS last_location,
            ps.hu_creation_date AT TIME ZONE 'Asia/Dubai' AS hu_creation_date_dubai,
            ps.hu_batch
        FROM movements m
        LEFT JOIN pallet_status ps ON ps.handling_unit = m.handling_unit
        WHERE m.handling_unit = %(hu)s
          AND m.movement_type IN ('STOCK_IN', 'STOCK_OUT')
        ORDER BY m.handling_unit, m.timestamp_utc DESC, m.id DESC
        LIMIT 1
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"hu": handling_unit})
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def upsert_pallet_status(
    handling_unit: str,
    current_status: str,
    event_id: str,
    last_movement_type: str,
    last_movement_time: str,
    indus_part: str = "",
    pallet_qty: str = "",
    last_location: str = "",
    hu_creation_date: str = None,
    hu_batch: str = None,
) -> None:
    sql = """
        INSERT INTO pallet_status (
            handling_unit, current_status, last_event_id,
            last_movement_type, last_movement_time,
            indus_part, pallet_qty, last_location,
            hu_creation_date, hu_batch
        ) VALUES (
            %(handling_unit)s, %(current_status)s, %(last_event_id)s,
            %(last_movement_type)s, %(last_movement_time)s,
            %(indus_part)s, %(pallet_qty)s, %(last_location)s,
            %(hu_creation_date)s, %(hu_batch)s
        )
        ON CONFLICT (handling_unit) DO UPDATE SET
            current_status     = EXCLUDED.current_status,
            last_event_id      = EXCLUDED.last_event_id,
            last_movement_type = EXCLUDED.last_movement_type,
            last_movement_time = EXCLUDED.last_movement_time,
            indus_part         = EXCLUDED.indus_part,
            pallet_qty         = EXCLUDED.pallet_qty,
            last_location      = EXCLUDED.last_location,
            hu_creation_date   = COALESCE(pallet_status.hu_creation_date, EXCLUDED.hu_creation_date),
            hu_batch           = COALESCE(pallet_status.hu_batch, EXCLUDED.hu_batch)
        WHERE pallet_status.last_movement_time < EXCLUDED.last_movement_time
    """
    params = dict(
        handling_unit=handling_unit,
        current_status=current_status,
        last_event_id=event_id,
        last_movement_type=last_movement_type,
        last_movement_time=_parse_ts(last_movement_time),
        indus_part=indus_part or "",
        pallet_qty=pallet_qty or "",
        last_location=last_location or "",
        hu_creation_date=_parse_ts(hu_creation_date) if hu_creation_date else None,
        hu_batch=hu_batch or None,
    )
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
    logger.debug(
        "upsert_pallet_status: hu=%s status=%s location=%s batch=%s",
        handling_unit, current_status, last_location, hu_batch
    )


def get_all_pallet_statuses() -> list[dict]:
    """Return latest status per HU with creation date and batch from pallet_status."""
    sql = """
        SELECT DISTINCT ON (m.handling_unit)
            m.handling_unit,
            CASE WHEN m.movement_type = 'STOCK_IN' THEN 'IN_WAREHOUSE'
                 ELSE 'OUT_OF_WAREHOUSE' END AS current_status,
            m.event_id          AS last_event_id,
            m.movement_type     AS last_movement_type,
            m.timestamp_utc AT TIME ZONE 'Asia/Dubai' AS last_movement_time_dubai,
            m.indus_part,
            m.pallet_qty,
            m.location          AS last_location,
            ps.hu_creation_date AT TIME ZONE 'Asia/Dubai' AS hu_creation_date_dubai,
            ps.hu_batch
        FROM movements m
        LEFT JOIN pallet_status ps ON ps.handling_unit = m.handling_unit
        WHERE m.movement_type IN ('STOCK_IN', 'STOCK_OUT')
        ORDER BY m.handling_unit, m.timestamp_utc DESC, m.id DESC
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]

# ---------------------------------------------------------------------------
# ivt_scans table
# ---------------------------------------------------------------------------

def ivt_find(code: str) -> dict | None:
    """Return the ivt_scans row for this code, or None if not found."""
    sql = "SELECT id, code, scanned_date, location FROM ivt_scans WHERE code = %(code)s"
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"code": code})
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def ivt_find_by_serial(serial: str, part_code: str) -> dict | None:
    sql = """
        SELECT id, code, scanned_date, location
        FROM ivt_scans
        WHERE SUBSTRING(code FROM 18 FOR 6) = %(serial)s
          AND SUBSTRING(code FROM 24 FOR 10) = %(part_code)s
        LIMIT 1
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"serial": serial, "part_code": part_code})
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def ivt_insert(code: str, scanned_date: str, location: str) -> None:
    """Insert a new IVT scan. Raises DbError on duplicate."""
    from datetime import datetime, timezone
    try:
        dt_value = datetime.strptime(scanned_date, '%d/%m/%Y %I:%M:%S %p').replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        dt_value = datetime.now(timezone.utc)
    sql = """
        INSERT INTO ivt_scans (code, scanned_date, location)
        VALUES (%(code)s, %(scanned_date)s, %(location)s)
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"code": code, "scanned_date": dt_value, "location": location})


# ---------------------------------------------------------------------------
# bdr_scans table
# ---------------------------------------------------------------------------

def bdr_find(code: str) -> dict | None:
    """Return the bdr_scans row for this code, or None if not found."""
    sql = """
        SELECT id, code, customer_code, part_number, year_2d, doy, serial,
               scanned_date, location
        FROM bdr_scans WHERE code = %(code)s
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"code": code})
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def bdr_find_by_serial(serial: str, part_number: str) -> dict | None:
    """Return the first bdr_scans row where serial and part_number match."""
    sql = """
        SELECT id, code, customer_code, part_number, year_2d, doy, serial,
               scanned_date, location
        FROM bdr_scans
        WHERE serial = %(serial)s
          AND part_number = %(part_number)s
        LIMIT 1
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"serial": serial.upper(), "part_number": part_number.upper()})
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def bdr_insert(
    code: str,
    customer_code: str,
    part_number: str,
    year_2d: str,
    doy: str,
    serial: str,
    scanned_date: str,
    location: str,
) -> None:
    """Insert a new BDR scan. Raises DbError on duplicate."""
    from datetime import datetime, timezone
    try:
        dt_value = datetime.strptime(scanned_date, '%d/%m/%Y %I:%M:%S %p').replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        dt_value = datetime.now(timezone.utc)
    sql = """
        INSERT INTO bdr_scans
            (code, customer_code, part_number, year_2d, doy, serial, scanned_date, location)
        VALUES
            (%(code)s, %(customer_code)s, %(part_number)s, %(year_2d)s,
             %(doy)s, %(serial)s, %(scanned_date)s, %(location)s)
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {
                "code":          code,
                "customer_code": customer_code,
                "part_number":   part_number.upper(),
                "year_2d":       year_2d,
                "doy":           doy,
                "serial":        serial.upper(),
                "scanned_date":  dt_value,
                "location":      location,
            })


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def ping() -> bool:
    """Return True if the database is reachable, False otherwise."""
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception as exc:
        logger.warning("DB ping failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _parse_ts(value) -> datetime:
    """
    Parse an ISO timestamp string (or datetime) to a timezone-aware datetime.
    Naive timestamps are assumed UTC.
    Falls back to datetime.now(UTC) on any parse error.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if not value:
        return datetime.now(timezone.utc)
    try:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        logger.warning("_parse_ts: could not parse %r, using now(UTC)", value)
        return datetime.now(timezone.utc)


def _row_to_dict(row) -> dict:
    """
    Convert a psycopg2 RealDictRow to a plain Python dict.
    datetime values are formatted as "D MMM YYYY, HH:MM:SS" (24-hour)
    for display in API responses and the admin dashboard.
    """
    if row is None:
        return {}
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, datetime):
            out[k] = v.strftime("%-d %b %Y, %H:%M:%S")
        else:
            out[k] = v
    return out

def fetch_one(sql: str, params=()) -> dict | None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def fetch_all(sql: str, params=()) -> list[dict]:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return [_row_to_dict(row) for row in rows]

def get_loading_by_idempotency_key(idempotency_key: str) -> dict | None:
    sql = """
        SELECT
            loading_id,
            idempotency_key,
            status,
            source_location,
            destination,
            operator_id,
            device_id,
            shipment_reference,
            supervisor_override,
            override_reason,
            item_count,
            created_at,
            completed_at
        FROM loading_headers
        WHERE idempotency_key = %s
        LIMIT 1
    """
    return fetch_one(sql, (idempotency_key,))

def get_loading_header(loading_id: str) -> dict | None:
    sql = """
        SELECT
            loading_id,
            idempotency_key,
            status,
            source_location,
            destination,
            operator_id,
            device_id,
            shipment_reference,
            supervisor_override,
            override_reason,
            item_count,
            created_at,
            completed_at
        FROM loading_headers
        WHERE loading_id = %s
        LIMIT 1
    """
    return fetch_one(sql, (loading_id,))

def get_loading_items(loading_id: str) -> list:
    sql = """
        SELECT
            li.loading_item_id,
            li.loading_id,
            li.handling_unit,
            li.sequence_no,
            li.resolution,
            li.stock_in_event_id,
            li.stock_out_event_id,
            li.indus_part,
            li.customer_part,
            li.pallet_qty,
            li.created_at,
            stock_out.timestamp_utc AS stock_out_timestamp,
            stock_out.location AS source_location,
            stock_out.to_location AS destination
        FROM loading_items li
        LEFT JOIN movements stock_out
            ON stock_out.event_id = li.stock_out_event_id
        WHERE li.loading_id = %s
        ORDER BY li.sequence_no ASC
    """
    return fetch_all(sql, (loading_id,))

def get_recent_loadings(limit: int = 100, status: str = "") -> list:
    params = []
    where = ""

    if status:
        where = "WHERE status = %s"
        params.append(status)

    params.append(limit)

    sql = f"""
        SELECT
            loading_id,
            idempotency_key,
            status,
            source_location,
            destination,
            operator_id,
            device_id,
            shipment_reference,
            supervisor_override,
            override_reason,
            item_count,
            created_at,
            completed_at
        FROM loading_headers
        {where}
        ORDER BY created_at DESC
        LIMIT %s
    """

    return fetch_all(sql, tuple(params))

@contextmanager
def db_transaction():
    """Use one pooled connection for an atomic transaction."""
    with _get_conn() as connection:
        yield connection

def get_customer_part_by_indus_part(indus_part: str) -> str:
    """
    Return the customer-part code mapped to an Indus material number.

    material_master.material_number is the Indus part key.
    material_master.customer_part is the customer code to show in Loading Review.
    """
    material_number = str(indus_part or "").strip().upper()

    if not material_number:
        return ""

    try:
        with db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT customer_part
                    FROM material_master
                    WHERE UPPER(TRIM(material_number)) = %s
                    LIMIT 1
                    """,
                    (material_number,),
                )

                row = cursor.fetchone()

                if not row:
                    return ""

                if isinstance(row, dict):
                    return str(row.get("customer_part") or "").strip()

                return str(row[0] or "").strip()

    except Exception:
        logger.exception(
            "Could not look up customer part for material number %s",
            material_number,
        )
        return ""
