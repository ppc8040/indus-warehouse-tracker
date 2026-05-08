"""
scripts/repair_crossloc_stock_out.py

Purpose
-------
Repairs phantom stock imbalances caused by STOCK_OUT rows that were
scanned via supervisor override at a location different from where the
pallet was originally stocked IN.

Three cases are handled:

  Case 1 — Classic cross-location override
  ─────────────────────────────────────────
  Pattern  : IN at zone X  →  OVERRIDE OUT at zone Y  (Y ≠ X)
  After    : No re-IN at zone Y after the OVERRIDE OUT  (or zone Y
             already has a real IN that was part of a separate cycle)
  Problem  : Zone X has an orphan IN (phantom +qty).
             Zone Y has an unmatched OUT (phantom -qty).
  Fix      : Insert STOCK_OUT at zone X (1 s before OVERRIDE OUT)
             Insert STOCK_IN  at zone Y (1 s before OVERRIDE OUT)
  Result   : Both zones are balanced.

  Case 3 — Cross-location override, zone Y already self-healed
  ──────────────────────────────────────────────────────────────
  Pattern  : IN at zone X  →  OVERRIDE OUT at zone Y  →  real IN at
             zone Y follows immediately (zone Y now balanced).
  Problem  : Zone X still has the orphan IN (phantom +qty).
             Zone Y is already balanced — no phantom negative.
  Fix      : Insert STOCK_OUT at zone X only (1 s before OVERRIDE OUT)
  Result   : Zone X cleared; zone Y untouched (already clean).

  Case 4 — Cross-location override + ghost HU misread on re-IN
  ──────────────────────────────────────────────────────────────
  Pattern  : Same as Case 1 for the original HU.  The re-IN after the
             OVERRIDE OUT was scanned on a subtly different HU number
             (e.g. 1100… instead of 1000… — OCR digit misread) rather
             than on the correct HU.  The ghost HU is flagged for
             manual review but is NOT auto-repaired.
  Fix      : Same as Case 1 for the original HU.
             Ghost HU is printed as a REVIEW_NEEDED warning.

Safety contract
---------------
  - ZERO existing rows are modified or deleted.
  - Only INSERT (to movements) is used.  pallet_status is reconciled
    via UPSERT only when the injected row becomes the HU's latest
    non-ERROR movement (rare — most HUs have subsequent real movements).
  - Runs part-by-part.  You choose which Indus Part to fix, review
    the full dry-run preview, and explicitly type YES to confirm before
    any write happens.
  - Every part is wrapped in its own transaction.  Any error triggers
    a full rollback for that part — DB never left in a partial state.
  - Safe to re-run: already-repaired HUs are skipped (idempotency guard
    checks for existing CROSSLOC_BACKFILL rows for the same HU and the
    same OVERRIDE OUT event_id).
  - All inserted rows carry:
      operator_id       = 'CROSSLOC_REPAIR'
      validation_status = 'CROSSLOC_BACKFILL'
    so they are filterable in the admin audit log and never confused
    with the BACKFILLED rows from backfill_missing_stock_in.py.

Usage
-----
    heroku run python scripts/repair_crossloc_stock_out.py --app indus-warehouse-tracker
    OR locally with DATABASE_URL in your .env

Requirements
------------
    psycopg2  (already in requirements.txt)
    DATABASE_URL environment variable
"""

import os
import sys
import uuid
from datetime import datetime, timezone, timedelta

import psycopg2
import psycopg2.extras


# ═══════════════════════════════════════════════════════════════════════════════
# CASE CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

CASE_1 = 'CASE_1'   # Classic cross-loc: insert OUT@X + IN@Y
CASE_2 = 'CASE_2'   # Multi-hop: zone Y self-healed BUT HU re-cycled through zones
                    # in rapid succession — walk the full chain and balance each leg
CASE_3 = 'CASE_3'   # Zone Y self-healed: insert OUT@X only
CASE_4 = 'CASE_4'   # Cross-loc + ghost HU re-IN: same as Case 1 + flag ghost


# ═══════════════════════════════════════════════════════════════════════════════
# DB CONNECTION
# ═══════════════════════════════════════════════════════════════════════════════

def get_connection():
    uri = os.environ.get('DATABASE_URL', '')
    if not uri:
        print('ERROR: DATABASE_URL environment variable is not set.')
        sys.exit(1)
    if uri.startswith('postgres://'):
        uri = uri.replace('postgres://', 'postgresql://', 1)
    try:
        conn = psycopg2.connect(uri, cursor_factory=psycopg2.extras.RealDictCursor)
        # autocommit=True so read queries never open an implicit transaction.
        # process_part() manages write transactions with explicit BEGIN/COMMIT/ROLLBACK.
        conn.autocommit = True
        return conn
    except psycopg2.Error as exc:
        print(f'ERROR: Could not connect to Postgres: {exc}')
        sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════════
# SQL
# ═══════════════════════════════════════════════════════════════════════════════

# ── Step 1: find all Indus Parts that have at least one affected HU ───────────
#
# An HU is affected when:
#   - It has a STOCK_OUT with validation_status = 'OVERRIDE'
#   - AND there exists a STOCK_IN for the same HU BEFORE that OUT
#     (so this is NOT a never-seen HU — that case is handled by
#      backfill_missing_stock_in.py)
#   - AND the STOCK_OUT location ≠ the most-recent prior STOCK_IN location
#   - AND no CROSSLOC_BACKFILL row already exists for this OVERRIDE OUT
#     (idempotency: the OUT's event_id is stored in the synthetic rows'
#      job_no field as a reference key)
FIND_AFFECTED_PARTS = """
    SELECT DISTINCT m_out.indus_part
    FROM   movements m_out
    WHERE  m_out.movement_type      = 'STOCK_OUT'
      AND  m_out.validation_status  = 'OVERRIDE'
      AND  EXISTS (
               -- there IS a prior STOCK_IN for this HU
               SELECT 1 FROM movements m_in
               WHERE  m_in.handling_unit = m_out.handling_unit
                 AND  m_in.movement_type = 'STOCK_IN'
                 AND  m_in.timestamp_utc < m_out.timestamp_utc
           )
      AND  NOT EXISTS (
               -- the most-recent prior IN is at a DIFFERENT location
               -- (we check this properly per-HU in Python, but this
               --  pre-filter removes all obviously non-cross-loc rows)
               SELECT 1 FROM movements m_in2
               WHERE  m_in2.handling_unit = m_out.handling_unit
                 AND  m_in2.movement_type = 'STOCK_IN'
                 AND  m_in2.timestamp_utc < m_out.timestamp_utc
                 AND  m_in2.location      = m_out.location
                 AND  m_in2.timestamp_utc = (
                          SELECT MAX(m_in3.timestamp_utc)
                          FROM   movements m_in3
                          WHERE  m_in3.handling_unit = m_out.handling_unit
                            AND  m_in3.movement_type = 'STOCK_IN'
                            AND  m_in3.timestamp_utc < m_out.timestamp_utc
                      )
           )
      AND  NOT EXISTS (
               -- idempotency: no repair already done for this OUT event
               SELECT 1 FROM movements m_fix
               WHERE  m_fix.handling_unit     = m_out.handling_unit
                 AND  m_fix.validation_status = 'CROSSLOC_BACKFILL'
                 AND  m_fix.job_no            = 'XREF:' || m_out.event_id
           )
    ORDER BY m_out.indus_part;
"""

# ── Step 2: fetch every affected OVERRIDE OUT for a specific Indus Part ───────
# Returns one row per rogue OVERRIDE OUT (multiple per HU are possible).
FIND_ROGUE_OUTS_FOR_PART = """
    SELECT
        m_out.event_id        AS out_event_id,
        m_out.handling_unit,
        m_out.indus_part,
        m_out.customer_part,
        m_out.pallet_qty,
        m_out.location        AS out_zone,
        m_out.from_location   AS recorded_from,
        m_out.to_location     AS recorded_to,
        m_out.timestamp_utc   AS out_ts,
        m_out.device_id,
        m_out.job_no          AS original_job_no
    FROM movements m_out
    WHERE m_out.movement_type      = 'STOCK_OUT'
      AND m_out.validation_status  = 'OVERRIDE'
      AND m_out.indus_part         = %(indus_part)s
      AND EXISTS (
              SELECT 1 FROM movements m_in
              WHERE  m_in.handling_unit = m_out.handling_unit
                AND  m_in.movement_type = 'STOCK_IN'
                AND  m_in.timestamp_utc < m_out.timestamp_utc
          )
      AND NOT EXISTS (
              SELECT 1 FROM movements m_fix
              WHERE  m_fix.handling_unit     = m_out.handling_unit
                AND  m_fix.validation_status = 'CROSSLOC_BACKFILL'
                AND  m_fix.job_no            = 'XREF:' || m_out.event_id
          )
    ORDER BY m_out.handling_unit, m_out.timestamp_utc;
"""

# ── Step 3: get the most-recent STOCK_IN before a given timestamp for an HU ──
GET_PRIOR_STOCK_IN = """
    SELECT location, timestamp_utc, indus_part, pallet_qty, event_id
    FROM   movements
    WHERE  handling_unit = %(hu)s
      AND  movement_type = 'STOCK_IN'
      AND  timestamp_utc < %(out_ts)s
    ORDER  BY timestamp_utc DESC
    LIMIT  1;
"""

# ── Step 4: check if zone Y already has a real STOCK_IN after the OVERRIDE OUT
#           (Case 3 detection — zone Y self-healed)
CHECK_ZONE_Y_REHEALED = """
    SELECT 1
    FROM   movements
    WHERE  handling_unit     = %(hu)s
      AND  movement_type     = 'STOCK_IN'
      AND  location          = %(out_zone)s
      AND  timestamp_utc     > %(out_ts)s
      AND  validation_status <> 'CROSSLOC_BACKFILL'
    LIMIT  1;
"""

# ── Step 5: detect Case 4 — is there a re-IN on a ghost HU after the OUT?
#           A ghost HU shares most digits with the original but differs
#           in the first digit (e.g. 1100... vs 1000...).  We look for
#           any HU that is NOT the original, scanned IN at out_zone
#           within 10 minutes after the OVERRIDE OUT, with same indus_part.

# ── Step 4b: detect Case 2 — are there MORE OVERRIDE OUTs for this HU
#            within 30 minutes of the first one?
#            If yes, the HU went through a rapid multi-hop session and
#            zone Y's self-heal IN is part of that chain, not a clean fix.
CHECK_SUBSEQUENT_OVERRIDE_OUTS = """
    SELECT event_id, location, timestamp_utc, from_location, to_location
    FROM   movements
    WHERE  handling_unit     = %(hu)s
      AND  movement_type     = 'STOCK_OUT'
      AND  validation_status = 'OVERRIDE'
      AND  timestamp_utc     > %(out_ts)s
      AND  timestamp_utc     <= %(out_ts)s + INTERVAL '30 minutes'
    ORDER  BY timestamp_utc;
"""

# ── Step 4c: for Case 2, get ALL movements for this HU within the rapid
#            session window so we can walk the full chain.
GET_SESSION_MOVEMENTS = """
    SELECT event_id, movement_type, location, timestamp_utc, validation_status
    FROM   movements
    WHERE  handling_unit     = %(hu)s
      AND  timestamp_utc     > %(session_start)s
      AND  timestamp_utc     <= %(session_start)s + INTERVAL '30 minutes'
      AND  validation_status <> 'CROSSLOC_BACKFILL'
    ORDER  BY timestamp_utc;
"""

CHECK_GHOST_HU = """
    SELECT DISTINCT handling_unit, location, timestamp_utc
    FROM   movements
    WHERE  handling_unit     <> %(hu)s
      AND  movement_type      = 'STOCK_IN'
      AND  location           = %(out_zone)s
      AND  indus_part         = %(indus_part)s
      AND  timestamp_utc      > %(out_ts)s
      AND  timestamp_utc      <= %(out_ts)s + INTERVAL '10 minutes'
    ORDER  BY timestamp_utc
    LIMIT  5;
"""

# ── Step 6: get the latest non-ERROR movement for an HU (for pallet_status) ──
GET_LATEST_MOVEMENT_FOR_HU = """
    SELECT event_id, movement_type, timestamp_utc,
           indus_part, pallet_qty, location
    FROM   movements
    WHERE  handling_unit  = %(hu)s
      AND  movement_type <> 'ERROR'
    ORDER  BY timestamp_utc DESC, id DESC
    LIMIT  1;
"""

# ── Inserts ───────────────────────────────────────────────────────────────────

INSERT_SYNTHETIC_ROW = """
    INSERT INTO movements (
        event_id, timestamp_utc, handling_unit,
        indus_part, customer_part, pallet_qty, job_no,
        movement_type, operator_id, device_id,
        sync_status, validation_status,
        location, from_location, to_location
    ) VALUES (
        %(event_id)s, %(timestamp_utc)s, %(handling_unit)s,
        %(indus_part)s, %(customer_part)s, %(pallet_qty)s, %(job_no)s,
        %(movement_type)s, 'CROSSLOC_REPAIR', %(device_id)s,
        'SYNCED', 'CROSSLOC_BACKFILL',
        %(location)s, '', ''
    );
"""

UPSERT_PALLET_STATUS = """
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
        last_location      = EXCLUDED.last_location;
"""


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _hr(char='─', width=72):
    print(char * width)


def _banner(title):
    _hr('═')
    print(f'  {title}')
    _hr('═')


def _make_event_id() -> str:
    return 'XLOC-' + str(uuid.uuid4()).upper()[:12]


def _ts_offset(ts, delta_seconds: int) -> datetime:
    """
    Return ts ± delta_seconds as a timezone-aware datetime.
    Accepts psycopg2 datetime objects or ISO strings.
    """
    if isinstance(ts, str):
        s = ts.replace('Z', '+00:00')
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            dt = datetime.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S')
            dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt + timedelta(seconds=delta_seconds)


def _prompt_yes(question: str) -> bool:
    answer = input(f'\n  {question} [type YES to confirm, anything else to skip]: ').strip()
    if answer.upper() == 'YES':
        return True
    print('  Skipped.')
    return False


def _classify_case(cur, hu, out_zone, out_ts, prior_in_location) -> str:
    """
    Determine which case applies to this rogue OVERRIDE OUT.

    Logic:
      1. If zone Y (out_zone) already has a real STOCK_IN after the OUT
         AND no ghost HU exists → Case 3 (zone Y self-healed)
      2. If zone Y already has a real STOCK_IN AND a ghost HU exists
         close to the OUT time → Case 4 (ghost HU)
      3. Otherwise → Case 1 (classic cross-loc, zone Y needs backfill IN)
    """
    cur.execute(CHECK_ZONE_Y_REHEALED, {'hu': hu, 'out_zone': out_zone, 'out_ts': out_ts})
    zone_y_healed = cur.fetchone() is not None

    cur.execute(CHECK_GHOST_HU, {
        'hu': hu,
        'out_zone': out_zone,
        'indus_part': '',   # filled by caller
        'out_ts': out_ts,
    })
    # ghost check is done with indus_part by caller; here we return the base case
    if zone_y_healed:
        return CASE_3  # caller may upgrade to CASE_4 if ghost found
    return CASE_1

def _check_subsequent_outs(cur, hu, out_ts) -> list:
    """Return list of OVERRIDE OUTs for this HU within 30 min after out_ts."""
    cur.execute(CHECK_SUBSEQUENT_OVERRIDE_OUTS, {'hu': hu, 'out_ts': out_ts})
    return list(cur.fetchall())


def _get_session_movements(cur, hu, session_start) -> list:
    """Return all movements for this HU in the 30-min session window."""
    cur.execute(GET_SESSION_MOVEMENTS, {'hu': hu, 'session_start': session_start})
    return list(cur.fetchall())

def _check_ghost(cur, hu, out_zone, indus_part, out_ts) -> list:
    """Return list of potential ghost HU rows (may be empty)."""
    cur.execute(CHECK_GHOST_HU, {
        'hu': hu,
        'out_zone': out_zone,
        'indus_part': indus_part,
        'out_ts': out_ts,
    })
    return list(cur.fetchall())


def _already_repaired(cur, hu, out_event_id) -> bool:
    """Idempotency: return True if CROSSLOC_BACKFILL rows exist for this OUT."""
    cur.execute(
        """
        SELECT 1 FROM movements
        WHERE handling_unit     = %(hu)s
          AND validation_status = 'CROSSLOC_BACKFILL'
          AND job_no            = %(xref)s
        LIMIT 1;
        """,
        {'hu': hu, 'xref': 'XREF:' + out_event_id},
    )
    return cur.fetchone() is not None


# ═══════════════════════════════════════════════════════════════════════════════
# DATA FETCHING
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_affected_parts(conn) -> list:
    with conn.cursor() as cur:
        cur.execute(FIND_AFFECTED_PARTS)
        return [r['indus_part'] for r in cur.fetchall()]


def fetch_rogue_outs(conn, indus_part: str) -> list:
    with conn.cursor() as cur:
        cur.execute(FIND_ROGUE_OUTS_FOR_PART, {'indus_part': indus_part})
        return list(cur.fetchall())


def fetch_prior_in(cur, hu, out_ts) -> dict | None:
    cur.execute(GET_PRIOR_STOCK_IN, {'hu': hu, 'out_ts': out_ts})
    row = cur.fetchone()
    return dict(row) if row else None


def fetch_latest_movement(cur, hu) -> dict | None:
    cur.execute(GET_LATEST_MOVEMENT_FOR_HU, {'hu': hu})
    row = cur.fetchone()
    return dict(row) if row else None


# ═══════════════════════════════════════════════════════════════════════════════
# ANALYSIS — classify every rogue OUT into a repair plan
# ═══════════════════════════════════════════════════════════════════════════════

def build_repair_plan(conn, rogue_outs: list) -> list:
    """
    For each rogue OVERRIDE OUT row, build a repair plan dict:

        {
          'case'          : CASE_1 | CASE_3 | CASE_4
          'hu'            : str
          'indus_part'    : str
          'customer_part' : str
          'pallet_qty'    : str
          'device_id'     : str
          'original_job_no': str
          'out_event_id'  : str  — reference to the rogue OUT row
          'out_ts'        : datetime
          'out_zone'      : str  — zone where OUT was wrongly scanned
          'in_zone'       : str  — zone where pallet was actually stored
          'inserts'       : list of dicts, each describing one row to INSERT
          'ghost_hus'     : list of ghost HU rows (Case 4 only, else [])
          'skip_reason'   : str | None  — set if this HU should be skipped
        }
    """
    plans = []

    with conn.cursor() as cur:
        for row in rogue_outs:
            hu           = row['handling_unit']
            out_event_id = row['out_event_id']
            out_zone     = (row['out_zone'] or '').strip().upper()
            out_ts       = row['out_ts']
            indus_part   = row['indus_part'] or ''
            customer_part= row['customer_part'] or ''
            pallet_qty   = row['pallet_qty'] or ''
            device_id    = row['device_id'] or ''
            original_job = row['original_job_no'] or ''

            plan = {
                'case'           : None,
                'hu'             : hu,
                'indus_part'     : indus_part,
                'customer_part'  : customer_part,
                'pallet_qty'     : pallet_qty,
                'device_id'      : device_id,
                'original_job_no': original_job,
                'out_event_id'   : out_event_id,
                'out_ts'         : out_ts,
                'out_zone'       : out_zone,
                'in_zone'        : None,
                'inserts'        : [],
                'ghost_hus'      : [],
                'subsequent_outs': [],
                'skip_reason'    : None,
            }

            # ── Idempotency guard ─────────────────────────────────────────
            if _already_repaired(cur, hu, out_event_id):
                plan['skip_reason'] = 'Already repaired (CROSSLOC_BACKFILL rows exist)'
                plans.append(plan)
                continue

            # ── Find prior STOCK_IN ───────────────────────────────────────
            prior_in = fetch_prior_in(cur, hu, out_ts)
            if not prior_in:
                plan['skip_reason'] = (
                    'No prior STOCK_IN found before the OVERRIDE OUT. '
                    'Run backfill_missing_stock_in.py first.'
                )
                plans.append(plan)
                continue

            in_zone = (prior_in['location'] or '').strip().upper()
            plan['in_zone'] = in_zone

            # ── Verify it IS actually cross-location ──────────────────────
            if in_zone == out_zone:
                plan['skip_reason'] = (
                    f'OVERRIDE OUT location ({out_zone}) matches prior IN '
                    f'location ({in_zone}) — not a cross-location case.'
                )
                plans.append(plan)
                continue
            # ── Skip if a Case 2 plan already covers this HU in this batch ─
            # When the same HU has multiple OVERRIDE OUTs in a rapid session,
            # the first OUT is repaired as Case 2 (which walks the full chain).
            # Subsequent OUTs in the same session must not generate duplicate
            # inserts — they are already balanced by the chain walk above.
            already_in_case2_batch = any(
                p['case'] == CASE_2
                and p['hu'] == hu
                and p['skip_reason'] is None
                for p in plans  # plans built so far in this loop
            )
            if already_in_case2_batch:
                plan['skip_reason'] = (
                    f'Covered by Case 2 chain repair of an earlier OVERRIDE OUT '
                    f'for the same HU in this session — no separate fix needed.'
                )
                plans.append(plan)
                continue
          
            # ── Detect Case 3: zone Y already self-healed ─────────────────
            cur.execute(CHECK_ZONE_Y_REHEALED, {
                'hu': hu, 'out_zone': out_zone, 'out_ts': out_ts,
            })
            zone_y_healed = cur.fetchone() is not None

            # ── Detect Case 4: ghost HU re-IN near the override OUT ───────
            ghost_hus = _check_ghost(cur, hu, out_zone, indus_part, out_ts)

            # ── Detect Case 2: zone Y self-healed BUT more OVERRIDE OUTs
            #    exist within 30 min — this is a rapid multi-hop session.
            subsequent_outs = _check_subsequent_outs(cur, hu, out_ts)

            # ── Assign case ───────────────────────────────────────────────
            if ghost_hus:
                plan['case'] = CASE_4
            elif zone_y_healed and subsequent_outs:
                plan['case'] = CASE_2
            elif zone_y_healed:
                plan['case'] = CASE_3
            else:
                plan['case'] = CASE_1

            plan['subsequent_outs'] = subsequent_outs

            plan['ghost_hus'] = ghost_hus

            # ── Build insert list ─────────────────────────────────────────
            # Timestamp for all synthetic rows: 1 second before the rogue OUT.
            # Using the same timestamp for both keeps them visually paired
            # in the movement log.  event_id uniqueness is guaranteed by UUID.
            synthetic_ts = _ts_offset(out_ts, -1)
            xref_job_no  = 'XREF:' + out_event_id   # links synthetics to the rogue OUT

            # Row A: STOCK_OUT at in_zone (X) — always inserted for all three cases
            # Clears the orphan IN at zone X.
            plan['inserts'].append({
                'event_id'     : _make_event_id(),
                'timestamp_utc': synthetic_ts,
                'handling_unit': hu,
                'indus_part'   : indus_part,
                'customer_part': customer_part,
                'pallet_qty'   : pallet_qty,
                'job_no'       : xref_job_no,
                'movement_type': 'STOCK_OUT',
                'device_id'    : device_id,
                'location'     : in_zone,
                '_label'       : f'STOCK_OUT @ {in_zone}  (clears orphan IN at zone X)',
            })

            # Row B: STOCK_IN at out_zone (Y) — inserted for Case 1 and Case 4 only.
            # Case 3 skips this because zone Y already has a real IN after the OUT.
            # Case 2 skips this too — zone Y already has a real IN, but we must
            # also walk the subsequent chain to ensure every leg is balanced.
            if plan['case'] in (CASE_1, CASE_4):
                plan['inserts'].append({
                    'event_id'     : _make_event_id(),
                    'timestamp_utc': synthetic_ts,
                    'handling_unit': hu,
                    'indus_part'   : indus_part,
                    'customer_part': customer_part,
                    'pallet_qty'   : pallet_qty,
                    'job_no'       : xref_job_no,
                    'movement_type': 'STOCK_IN',
                    'device_id'    : device_id,
                    'location'     : out_zone,
                    '_label'       : f'STOCK_IN  @ {out_zone}  (matches OVERRIDE OUT at zone Y)',
                })

            # ── Case 2: walk the full rapid session chain ─────────────────
            # The zone Y self-heal IN is real, so that leg is balanced.
            # We now step through all movements in the 30-min session window
            # and insert a synthetic IN for any location that has an OVERRIDE
            # OUT with no prior IN in that session leg.
            if plan['case'] == CASE_2:
                session_moves = _get_session_movements(cur, hu, out_ts)
                # Walk chain sequentially — only mark a zone as "covered" when
                # we actually encounter its IN in time order, not by pre-scanning.
                # This correctly catches OUTs that precede their paired IN.
                session_in_zones = set()

                for sm in session_moves:
                    sm_type = (sm['movement_type'] or '').upper()
                    sm_loc  = (sm['location'] or '').strip().upper()
                    sm_ts   = sm['timestamp_utc']
                    if sm_type == 'STOCK_IN':
                        session_in_zones.add(sm_loc)
                    elif sm_type == 'STOCK_OUT' and sm_loc not in session_in_zones:
                    if sm_type == 'STOCK_OUT' and sm_loc not in session_in_zones:
                        # This OUT in the chain has no matching IN — insert one
                        chain_synthetic_ts = _ts_offset(sm_ts, -1)
                        plan['inserts'].append({
                            'event_id'     : _make_event_id(),
                            'timestamp_utc': chain_synthetic_ts,
                            'handling_unit': hu,
                            'indus_part'   : indus_part,
                            'customer_part': customer_part,
                            'pallet_qty'   : pallet_qty,
                            'job_no'       : xref_job_no,
                            'movement_type': 'STOCK_IN',
                            'device_id'    : device_id,
                            'location'     : sm_loc,
                            '_label'       : (
                                f'STOCK_IN  @ {sm_loc}  '
                                f'(Case 2 chain leg — unmatched OUT in session)'
                            ),
                        })
                        session_in_zones.add(sm_loc)  # mark as now covered

            plans.append(plan)

    return plans


# ═══════════════════════════════════════════════════════════════════════════════
# DISPLAY — dry-run preview
# ═══════════════════════════════════════════════════════════════════════════════

CASE_LABELS = {
    CASE_1: 'Case 1 — Classic cross-loc (OUT@Y balanced, orphan IN@X cleared)',
    CASE_2: 'Case 2 — Multi-hop chain (zone Y self-healed but HU re-cycled; full chain balanced)',
    CASE_3: 'Case 3 — Zone Y self-healed (orphan IN@X only needs clearing)',
    CASE_4: 'Case 4 — Cross-loc + ghost HU re-IN (same as Case 1 + flag)',
}


def print_dry_run(plans: list) -> tuple[int, int]:
    """
    Print the full dry-run preview.
    Returns (total_inserts, total_skips).
    """
    total_inserts = 0
    total_skips   = 0

    for p in plans:
        hu = p['hu']

        if p['skip_reason']:
            total_skips += 1
            print(f'  ⚠  {hu} — SKIPPED: {p["skip_reason"]}')
            continue

        case_label = CASE_LABELS.get(p['case'], p['case'])
        print()
        print(f'  HU      : {hu}')
        print(f'  Part    : {p["indus_part"]}  qty={p["pallet_qty"]}')
        print(f'  Case    : {case_label}')
        print(f'  IN zone : {p["in_zone"]}  →  OUT zone (override): {p["out_zone"]}')
        print(f'  OUT evt : {p["out_event_id"]}  @ {str(p["out_ts"])[:19]}')
        print(f'  Rows to insert:')
        for ins in p['inserts']:
            ts_str = str(ins['timestamp_utc'])[:19]
            print(f'      [{ins["movement_type"]:9s}]  zone={ins["location"]:10s}  '
                  f'ts={ts_str}  event={ins["event_id"]}')
            print(f'                    → {ins["_label"]}')
            total_inserts += 1
        if p.get('subsequent_outs') and p['case'] == CASE_2:
            print(f'  ⚑  CASE 2 — Rapid multi-hop session detected. '
                  f'Subsequent OVERRIDE OUTs in chain:')
            for s in p['subsequent_outs']:
                print(f'       OUT event: {s["event_id"]}  '
                      f'loc={s["location"]}  '
                      f'ts={str(s["timestamp_utc"])[:19]}')
            print(f'     Full session chain has been walked and balanced above.')

        if p['ghost_hus']:
            print(f'  ⚑  REVIEW_NEEDED — ghost HU(s) detected at {p["out_zone"]}:')
            for g in p['ghost_hus']:
                print(f'       ghost HU: {g["handling_unit"]}  '
                      f'ts: {str(g["timestamp_utc"])[:19]}')
            print(f'     These ghost HUs are NOT auto-repaired. '
                  f'Review them manually in the admin portal.')

        _hr('-', 64)

    return total_inserts, total_skips


# ═══════════════════════════════════════════════════════════════════════════════
# EXECUTION — write the repairs inside a transaction
# ═══════════════════════════════════════════════════════════════════════════════

def process_part(conn, indus_part: str, plans: list) -> dict:
    """
    Execute all inserts for a single Indus Part inside one transaction.
    Only plans without a skip_reason are processed.

    Returns summary dict:
        inserted     int
        skipped      int
        ps_updated   int
        rolled_back  bool
    """
    summary = {'inserted': 0, 'skipped': 0, 'ps_updated': 0, 'rolled_back': False}

    cur = conn.cursor()
    try:
        cur.execute('BEGIN')

        for p in plans:
            hu = p['hu']

            if p['skip_reason']:
                summary['skipped'] += 1
                continue

            # ── Double-check idempotency inside the transaction ───────────
            if _already_repaired(cur, hu, p['out_event_id']):
                print(f'  ⚠  {hu} — already repaired inside transaction, skipping.')
                summary['skipped'] += 1
                continue

            # ── Insert each synthetic row ─────────────────────────────────
            for ins in p['inserts']:
                params = {
                    k: v for k, v in ins.items()
                    if not k.startswith('_')   # strip display-only keys
                }
                cur.execute(INSERT_SYNTHETIC_ROW, params)
                summary['inserted'] += 1
                print(f'  ✓  [{ins["movement_type"]:9s}] @ {ins["location"]:10s}  '
                      f'hu={hu}  event={ins["event_id"]}')

            # ── Reconcile pallet_status (only if this HU has no later moves) ─
            # After inserting, fetch the latest non-ERROR movement.
            # If it is one of OUR synthetic rows, update pallet_status.
            # If a real movement already exists after our synthetics, leave
            # pallet_status alone — the real movement is authoritative.
            cur.execute(GET_LATEST_MOVEMENT_FOR_HU, {'hu': hu})
            latest = cur.fetchone()
            if latest:
                latest = dict(latest)
                if latest.get('validation_status') == 'CROSSLOC_BACKFILL':
                    mv_type = (latest['movement_type'] or '').upper()
                    current_status = (
                        'IN_WAREHOUSE' if mv_type == 'STOCK_IN'
                        else 'OUT_OF_WAREHOUSE'
                    )
                    cur.execute(UPSERT_PALLET_STATUS, {
                        'handling_unit'     : hu,
                        'current_status'    : current_status,
                        'last_event_id'     : latest['event_id'],
                        'last_movement_type': latest['movement_type'],
                        'last_movement_time': str(latest['timestamp_utc']),
                        'indus_part'        : latest['indus_part'] or '',
                        'pallet_qty'        : latest['pallet_qty'] or '',
                        'last_location'     : latest['location'] or '',
                    })
                    summary['ps_updated'] += 1
                    print(f'        pallet_status → status={current_status}  '
                          f'loc={latest["location"]}')

        cur.execute('COMMIT')
        print(f'\n  ✓  Transaction committed for part: {indus_part}')

    except Exception as exc:
        cur.execute('ROLLBACK')
        summary['rolled_back'] = True
        print(f'\n  ✗  ERROR — full rollback for part: {indus_part}')
        print(f'     Exception: {exc}')
    finally:
        cur.close()

    return summary


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN INTERACTIVE FLOW
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    _banner('repair_crossloc_stock_out.py')
    print()
    print('  PURPOSE:')
    print('  Backfills synthetic IN/OUT pairs to balance cross-location')
    print('  override STOCK_OUTs.  Handles Cases 1, 3 and 4.')
    print()
    print('  SAFETY:')
    print('  ✓  ZERO existing rows are modified or deleted.')
    print('  ✓  Runs part-by-part with explicit YES confirmation.')
    print('  ✓  Each part is a separate transaction — error = full rollback.')
    print('  ✓  Already-repaired HUs are automatically skipped (re-run safe).')
    print('  ✓  Synthetic rows tagged CROSSLOC_BACKFILL for clean audit trail.')
    print('  ✓  Ghost HUs (Case 4) are flagged only — never auto-touched.')
    _hr()

    conn = get_connection()
    print('  ✓  Connected to database.\n')

    # ── Find affected parts ────────────────────────────────────────────────
    print('[SCAN] Scanning for cross-location override STOCK_OUTs...')
    affected_parts = fetch_affected_parts(conn)

    if not affected_parts:
        print()
        print('  ✓  No cross-location override STOCK_OUTs found. Nothing to fix.')
        conn.close()
        sys.exit(0)

    print(f'  Found {len(affected_parts)} Indus Part(s) with affected HUs:\n')
    for i, part in enumerate(affected_parts, 1):
        print(f'    [{i}]  {part}')
    print('    [A]  Process ALL parts in sequence')
    print('    [Q]  Quit without making any changes')
    print()

    grand_total = {'inserted': 0, 'skipped': 0, 'ps_updated': 0, 'errors': 0}

    while True:
        choice = input('  Enter part number, A for all, or Q to quit: ').strip().upper()

        if choice == 'Q':
            print('\n  Quit requested. No changes made.')
            conn.close()
            sys.exit(0)

        if choice == 'A':
            parts_to_process = list(affected_parts)
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(affected_parts):
                parts_to_process = [affected_parts[idx]]
            else:
                print(f'  Invalid choice. Enter 1–{len(affected_parts)}, A, or Q.')
                continue
        else:
            print('  Invalid input. Enter a number, A, or Q.')
            continue

        # ── Build repair plans for selected parts ──────────────────────────
        print()
        _hr()
        print('  DRY-RUN PREVIEW — the following rows will be inserted:')
        _hr()

        all_part_plans  = []   # list of (indus_part, [plans])
        grand_inserts   = 0
        grand_skips     = 0

        for part in parts_to_process:
            rogue_outs = fetch_rogue_outs(conn, part)
            if not rogue_outs:
                continue
            plans = build_repair_plan(conn, rogue_outs)

            new_plans  = [p for p in plans if not p['skip_reason']]
            skip_plans = [p for p in plans if p['skip_reason']]

            if new_plans or skip_plans:
                print(f'\n  ── Part: {part}  ({len(new_plans)} HU(s) to repair, '
                      f'{len(skip_plans)} skipped) ──')
                inserts, skips = print_dry_run(plans)
                grand_inserts += inserts
                grand_skips   += skips
                all_part_plans.append((part, plans))

        if grand_inserts == 0:
            print()
            print('  ✓  All affected HUs already repaired. Nothing to do.')
            print('  Returning to menu...\n')
            continue

        print()
        _hr()
        print('  SUMMARY OF PENDING CHANGES:')
        print(f'    Rows to INSERT                     : {grand_inserts}')
        print(f'    HUs skipped (already done / n/a)   : {grand_skips}')
        print(f'    Existing rows modified              : 0  ← guaranteed')
        print(f'    Existing rows deleted               : 0  ← guaranteed')
        _hr()

        if not _prompt_yes(
            f'Proceed with inserting {grand_inserts} synthetic row(s)?'
        ):
            print('  No changes made. Returning to menu.\n')
            continue

        # ── Execute ────────────────────────────────────────────────────────
        for part, plans in all_part_plans:
            active_plans = [p for p in plans if not p['skip_reason']]
            if not active_plans:
                continue
            print()
            _hr()
            print(f'  Processing part: {part}  ({len(active_plans)} HU(s))')
            _hr()

            result = process_part(conn, part, active_plans)
            grand_total['inserted']   += result['inserted']
            grand_total['skipped']    += result['skipped']
            grand_total['ps_updated'] += result['ps_updated']
            if result['rolled_back']:
                grand_total['errors'] += 1

        # ── Final summary ──────────────────────────────────────────────────
        print()
        _banner('COMPLETE')
        print(f'  Synthetic rows inserted  : {grand_total["inserted"]}')
        print(f'  pallet_status updated    : {grand_total["ps_updated"]}')
        print(f'  HUs skipped              : {grand_total["skipped"]}')
        print(f'  Parts with errors        : {grand_total["errors"]}')
        print()
        if grand_total['errors'] == 0:
            print('  ✓  All changes committed successfully.')
        else:
            print('  ⚠  Some parts had errors and were rolled back.')
            print('     Re-run the script to retry those parts.')
        print()
        print("  AUDIT: SELECT * FROM movements WHERE validation_status = 'CROSSLOC_BACKFILL'")
        print("         Each synthetic row's job_no = 'XREF:<original_out_event_id>'")
        print('         so you can trace every insert back to the rogue OUT row.')
        _hr('═')
        break

    conn.close()


if __name__ == '__main__':
    run()
