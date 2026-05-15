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
      last_location       TEXT
  )

  ivt_scans (
      id           SERIAL PRIMARY KEY,
      code         TEXT UNIQUE NOT NULL,
      scanned_date TEXT NOT NULL,
      location     TEXT NOT NULL
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
            location, from_location, to_location
        ) VALUES (
            %(event_id)s, %(timestamp_utc)s, %(handling_unit)s, %(indus_part)s,
            %(customer_part)s, %(pallet_qty)s, %(job_no)s, %(movement_type)s,
            %(operator_id)s, %(device_id)s, %(sync_status)s, %(validation_status)s,
            %(location)s, %(from_location)s, %(to_location)s
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
    """Return current status for a single HU, derived from movements
    with timestamp DESC, id DESC tiebreak."""
    sql = """
        SELECT DISTINCT ON (handling_unit)
            handling_unit,
            CASE WHEN movement_type = 'STOCK_IN' THEN 'IN_WAREHOUSE'
                 ELSE 'OUT_OF_WAREHOUSE' END AS current_status,
            event_id          AS last_event_id,
            movement_type     AS last_movement_type,
            timestamp_utc AT TIME ZONE 'Asia/Dubai' AS last_movement_time_dubai,
            indus_part,
            pallet_qty,
            location          AS last_location
        FROM movements
        WHERE handling_unit = %(hu)s
          AND movement_type IN ('STOCK_IN', 'STOCK_OUT')
        ORDER BY handling_unit, timestamp_utc DESC, id DESC
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
) -> None:
    """
    Insert or update the pallet_status row for this HU.

    The ON CONFLICT DO UPDATE is guarded by a WHERE clause that checks
    the incoming last_movement_time is strictly newer than what is already
    stored. This prevents a late-arriving or out-of-order event (e.g. an
    OUT scan processed after an IN scan that happened seconds later) from
    overwriting the correct current state.

    If two events share the exact same timestamp, the existing row wins
    (no update). In practice this is extremely rare; the id-based tiebreak
    in get_last_movement_for_hu handles the display/query side of the
    same-second edge case.
    """
    sql = """
        INSERT INTO pallet_status (
            handling_unit, current_status, last_event_id,
            last_movement_type, last_movement_time,
            indus_part, pallet_qty, last_location
        ) VALUES (
            %(handling_unit)s, %(current_status)s, %(last_event_id)s,
            %(last_movement_type)s, %(last_movement_time)s,
            %(indus_part)s, %(pallet_qty)s, %(last_location)s
        )
        ON CONFLICT (handling_unit) DO UPDATE SET
            current_status     = EXCLUDED.current_status,
            last_event_id      = EXCLUDED.last_event_id,
            last_movement_type = EXCLUDED.last_movement_type,
            last_movement_time = EXCLUDED.last_movement_time,
            indus_part         = EXCLUDED.indus_part,
            pallet_qty         = EXCLUDED.pallet_qty,
            last_location      = EXCLUDED.last_location
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
    )
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
    logger.debug("upsert_pallet_status: hu=%s status=%s location=%s", handling_unit, current_status, last_location)


def get_all_pallet_statuses() -> list[dict]:
    """Return latest status per HU, derived live from movements using
    timestamp DESC, id DESC tiebreak — matches movements dashboard exactly."""
    sql = """
        SELECT DISTINCT ON (handling_unit)
            handling_unit,
            CASE WHEN movement_type = 'STOCK_IN' THEN 'IN_WAREHOUSE'
                 ELSE 'OUT_OF_WAREHOUSE' END AS current_status,
            event_id          AS last_event_id,
            movement_type     AS last_movement_type,
            timestamp_utc AT TIME ZONE 'Asia/Dubai' AS last_movement_time_dubai,
            indus_part,
            pallet_qty,
            location          AS last_location
        FROM movements
        WHERE movement_type IN ('STOCK_IN', 'STOCK_OUT')
        ORDER BY handling_unit, timestamp_utc DESC, id DESC
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


def ivt_find_by_serial(serial: str) -> dict | None:
    """
    Return the ivt_scans row whose serial segment (positions 17-22 of the
    33-digit code) matches `serial`, or None if not found.

    Uses SUBSTRING(code FROM 18 FOR 6) — SQL positions are 1-based, so
    position 18 corresponds to Python index 17.

    This enforces serial-level uniqueness: the same physical unit cannot
    be scanned twice even if the surrounding code differs (e.g. different
    part code or date segment).
    """
    sql = """
        SELECT id, code, scanned_date, location
        FROM ivt_scans
        WHERE SUBSTRING(code FROM 18 FOR 6) = %(serial)s
        LIMIT 1
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"serial": serial})
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def ivt_insert(code: str, scanned_date: str, location: str) -> None:
    """Insert a new IVT scan. Raises DbError on duplicate."""
    sql = """
        INSERT INTO ivt_scans (code, scanned_date, location)
        VALUES (%(code)s, %(scanned_date)s, %(location)s)
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"code": code, "scanned_date": scanned_date, "location": location})
    logger.debug("ivt_insert: code=%s location=%s", code, location)


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
    datetime values are formatted as "D MMM YYYY, HH:MM" (24-hour, no seconds)
    for display in API responses and the admin dashboard.
    Seconds are preserved in the database (TIMESTAMPTZ) but not shown.
    """
    if row is None:
        return {}
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, datetime):
            # Format: "27 Apr 2026, 09:33" — day without leading zero,
            # abbreviated month, 24-hour clock, no seconds.
            out[k] = v.strftime("%-d %b %Y, %H:%M")
        else:
            out[k] = v
    return out
