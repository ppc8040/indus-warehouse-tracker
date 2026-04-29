"""
Microsoft Graph API client — worksheet range operations.

Matches the EXACT pattern from Indus-Open-Order-Report-CUST.py:
  - MSAL ConfidentialClientApplication + client_credentials
  - Workbook sessions: createSession / closeSession with persistChanges=True
  - URL: /sites/{site_id}/drive/root:/{filename}:/workbook/worksheets('{name}')
  - Range reads/writes: usedRange, range(address='A2:G100')
  - All operations happen inside a session, session always closed in finally

No named tables — pure worksheet + range operations.
All secrets server-side only.

Session strategy
────────────────
Microsoft Graph limits a workbook to 5 concurrent open sessions.
A single movement flow previously opened 3-4 sessions in series which,
under concurrent workers, could easily exceed that limit and produce
"Could not open workbook session" errors in the Error_Log.

Fix: every public function now has an internal _impl variant that accepts
an optional external (session_id, headers) tuple.  When a caller supplies
a session the function uses it directly and does NOT close it.  When no
session is supplied the function opens its own session and closes it in a
finally block — the original behaviour, kept intact for every standalone
caller (admin panel, health-check, duplicate check, etc.).

movement_service.process_movement() opens ONE session, passes it through
all Graph calls in that request, and closes it in its own finally block.
"""
import time
import logging

import msal
import requests
from flask import current_app

logger = logging.getLogger(__name__)


def _normalise_cell(val):
    """
    Normalise a cell value for comparison.
    Excel/Graph API returns large integers as floats (e.g. 1.0000000000083916e+17).
    This converts them back to clean integer strings so HU lookups work correctly.
    """
    if val is None:
        return ''
    if isinstance(val, float):
        if val == int(val):
            return str(int(val)).strip().upper()
    return str(val).strip().upper()


_token_cache = {'access_token': None, 'expires_at': 0}
_http = requests.Session()


def _safe_row(values_list):
    """
    Convert any value in a row that looks like a large integer (>15 digits)
    to a string prefixed with a single quote so Excel stores it as TEXT,
    not a float. This prevents Excel from corrupting long HU numbers
    (IEEE 754 doubles only have 15 significant digits).
    """
    safe = []
    for v in values_list:
        if v is not None:
            s = str(v).strip()
            if s.isdigit() and len(s) > 15:
                safe.append("'" + s)
            else:
                safe.append(v)
        else:
            safe.append(v)
    return safe


class GraphAPIError(Exception):
    def __init__(self, message, status_code=500, details=""):
        self.message = message
        self.status_code = status_code
        self.details = details
        super().__init__(self.message)


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTH  (mirrors your reference exactly)
# ═══════════════════════════════════════════════════════════════════════════════

def _get_token():
    global _token_cache
    if _token_cache['access_token'] and _token_cache['expires_at'] > time.time() + 60:
        return _token_cache['access_token']

    tenant = current_app.config['GRAPH_TENANT_ID']
    cid = current_app.config['GRAPH_CLIENT_ID']
    secret = current_app.config['GRAPH_CLIENT_SECRET']
    if not all([tenant, cid, secret]):
        raise GraphAPIError("Graph credentials not configured.", 503)

    authority = f"https://login.microsoftonline.com/{tenant}"
    app = msal.ConfidentialClientApplication(cid, authority=authority, client_credential=secret)

    result = app.acquire_token_silent(scopes=['https://graph.microsoft.com/.default'], account=None)
    if not result:
        result = app.acquire_token_for_client(scopes=['https://graph.microsoft.com/.default'])

    if 'access_token' not in result:
        desc = result.get('error_description', 'Unknown auth error')
        logger.error(f"Token failed: {desc}")
        raise GraphAPIError("Authentication failed.", 503, desc)

    _token_cache['access_token'] = result['access_token']
    _token_cache['expires_at'] = time.time() + result.get('expires_in', 3600)
    logger.info("Graph token acquired.")
    return _token_cache['access_token']


# ═══════════════════════════════════════════════════════════════════════════════
#  URL BUILDER  (drive/root:/{filename}: pattern)
# ═══════════════════════════════════════════════════════════════════════════════

def _wb_url():
    sid = current_app.config['SHAREPOINT_SITE_ID']
    fn = current_app.config['WORKBOOK_FILENAME']
    return f"https://graph.microsoft.com/v1.0/sites/{sid}/drive/root:/{fn}:/workbook"


def _wb_url_for(filename):
    """Build workbook base URL for any filename in the same SharePoint drive."""
    sid = current_app.config['SHAREPOINT_SITE_ID']
    return f"https://graph.microsoft.com/v1.0/sites/{sid}/drive/root:/{filename}:/workbook"


def _ws_url(sheet):
    return f"{_wb_url()}/worksheets('{sheet}')"


def _ws_url_for(filename, sheet):
    """Build worksheet URL for any filename + sheet combination."""
    return f"{_wb_url_for(filename)}/worksheets('{sheet}')"


# ═══════════════════════════════════════════════════════════════════════════════
#  SESSION MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

def _open_session():
    """Create persistent workbook session. Returns (session_id, headers_dict)."""
    token = _get_token()
    url = f"{_wb_url()}/createSession"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    timeout = current_app.config.get('GRAPH_REQUEST_TIMEOUT', 30)

    resp = _http.post(url, headers=headers, json={"persistChanges": True}, timeout=timeout)
    if resp.status_code not in (200, 201):
        raise GraphAPIError("Could not open workbook session.", resp.status_code, resp.text[:300])

    sid = resp.json().get("id")
    logger.info(f"Session opened: {sid[:16]}...")
    return sid, {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "workbook-session-id": sid,
    }


def _open_session_for(filename):
    """
    Open a workbook session for any file (not just WORKBOOK_FILENAME).
    Returns (session_id, headers_dict).
    """
    token = _get_token()
    url = f"{_wb_url_for(filename)}/createSession"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    timeout = current_app.config.get('GRAPH_REQUEST_TIMEOUT', 30)

    resp = _http.post(url, headers=headers, json={"persistChanges": False}, timeout=timeout)
    if resp.status_code not in (200, 201):
        raise GraphAPIError(
            f"Could not open workbook session for '{filename}'.",
            resp.status_code, resp.text[:300]
        )

    sid = resp.json().get("id")
    logger.info(f"Session opened for '{filename}': {sid[:16]}...")
    return sid, {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "workbook-session-id": sid,
    }


def _close_session_for(filename, session_id):
    """Close a session for any workbook (best-effort, never raises)."""
    try:
        token = _get_token()
        _http.post(
            f"{_wb_url_for(filename)}/closeSession",
            headers={"Authorization": f"Bearer {token}", "workbook-session-id": session_id},
            timeout=10,
        )
        logger.info(f"Session closed for '{filename}'.")
    except Exception as e:
        logger.warning(f"Session close failed for '{filename}' (non-fatal): {e}")


def _close_session(session_id):
    """Close session (best-effort, never raises)."""
    try:
        token = _get_token()
        _http.post(
            f"{_wb_url()}/closeSession",
            headers={"Authorization": f"Bearer {token}", "workbook-session-id": session_id},
            timeout=10,
        )
        logger.info("Session closed.")
    except Exception as e:
        logger.warning(f"Session close failed (non-fatal): {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  LOW-LEVEL REQUEST  with retry on 429 / 5xx
# ═══════════════════════════════════════════════════════════════════════════════

def _req(method, url, headers, json_data=None, attempt=0):
    max_r = current_app.config.get('GRAPH_MAX_RETRIES', 3)
    timeout = current_app.config.get('GRAPH_REQUEST_TIMEOUT', 30)
    try:
        resp = _http.request(method=method, url=url, headers=headers, json=json_data, timeout=timeout)

        if resp.status_code == 429:
            wait = int(resp.headers.get('Retry-After', 5))
            if attempt < max_r:
                logger.warning(f"Throttled, wait {wait}s (attempt {attempt+1})")
                time.sleep(min(wait, 30))
                return _req(method, url, headers, json_data, attempt+1)
            raise GraphAPIError("Rate limited. Try again later.", 429)

        if resp.status_code >= 500 and attempt < max_r:
            time.sleep(2 ** attempt)
            return _req(method, url, headers, json_data, attempt+1)

        if resp.status_code == 404:
            raise GraphAPIError("Resource not found.", 404, resp.text[:300])
        if resp.status_code >= 400:
            raise GraphAPIError(f"Graph error ({resp.status_code})", resp.status_code, resp.text[:300])
        if resp.status_code == 204:
            return {}
        return resp.json()

    except requests.exceptions.Timeout:
        if attempt < max_r:
            return _req(method, url, headers, json_data, attempt+1)
        raise GraphAPIError("Request timed out.", 504)
    except requests.exceptions.ConnectionError:
        if attempt < max_r:
            time.sleep(2 ** attempt)
            return _req(method, url, headers, json_data, attempt+1)
        raise GraphAPIError("Cannot reach Graph API.", 503)


# ═══════════════════════════════════════════════════════════════════════════════
#  COLUMN HELPER
# ═══════════════════════════════════════════════════════════════════════════════

def _col_letter(n):
    """1-based column number → Excel letter (1→A, 27→AA)."""
    s = ""
    while n > 0:
        n -= 1
        s = chr(n % 26 + ord('A')) + s
        n //= 26
    return s


# ═══════════════════════════════════════════════════════════════════════════════
#  INTERNAL IMPLEMENTATIONS  (accept an external session tuple)
#
#  Each _impl function takes an optional `ext_session` parameter which is
#  (session_id, headers_dict).  When provided, the function uses that session
#  and NEVER closes it — the caller is responsible for cleanup.  When None,
#  the function opens and closes its own session exactly as before.
# ═══════════════════════════════════════════════════════════════════════════════

def _read_all_rows_impl(sheet_name, ext_session=None):
    """
    Read entire used range from a worksheet.
    Returns (headers: list[str], rows: list[list]).
    """
    own_session = ext_session is None
    sid, h = _open_session() if own_session else ext_session
    try:
        url = f"{_ws_url(sheet_name)}/usedRange"
        data = _req('GET', url, h)
        values = data.get('values', [])
        if not values:
            return [], []
        headers = [str(v) if v is not None else '' for v in values[0]]
        rows = values[1:] if len(values) > 1 else []
        logger.info(f"Read {len(rows)} rows from '{sheet_name}'")
        return headers, rows
    finally:
        if own_session:
            _close_session(sid)


def _find_row_as_dict_impl(sheet_name, col_name, search_value, ext_session=None):
    """
    Find first row where column named col_name matches search_value.
    Returns dict or None.
    """
    own_session = ext_session is None
    sid, h = _open_session() if own_session else ext_session
    try:
        url = f"{_ws_url(sheet_name)}/usedRange"
        data = _req('GET', url, h)
        values = data.get('values', [])
        if len(values) < 2:
            return None
        headers = [str(v) if v is not None else '' for v in values[0]]
        if col_name not in headers:
            logger.warning(f"Column '{col_name}' not found in '{sheet_name}'")
            return None
        col_idx = headers.index(col_name)
        needle = str(search_value).strip().upper()
        for i in range(1, len(values)):
            row = values[i]
            if col_idx < len(row):
                cell = _normalise_cell(row[col_idx])
                if cell == needle:
                    d = {}
                    for j, hdr in enumerate(headers):
                        d[hdr] = row[j] if j < len(row) else None
                    return d
        return None
    finally:
        if own_session:
            _close_session(sid)


def _append_row_impl(sheet_name, values_list, num_cols, ext_session=None):
    """
    Append a single data row after the last REAL data row (keyed on column A).
    """
    own_session = ext_session is None
    sid, h = _open_session() if own_session else ext_session
    try:
        url = f"{_ws_url(sheet_name)}/usedRange"
        data = _req('GET', url, h)
        all_values = data.get('values', [])

        if not all_values:
            next_row = 1
        else:
            last_data_row = 0
            for i, row in enumerate(all_values, start=1):
                first_cell = row[0] if len(row) > 0 else None
                if first_cell is not None and str(first_cell).strip() != '':
                    last_data_row = i
            next_row = last_data_row + 1 if last_data_row > 0 else 1

        addr = f"A{next_row}:{_col_letter(num_cols)}{next_row}"
        write_url = f"{_ws_url(sheet_name)}/range(address='{addr}')"
        _req('PATCH', write_url, h, json_data={"values": [_safe_row(values_list)]})
        logger.info(f"Appended row {next_row} to '{sheet_name}'")
        return next_row
    finally:
        if own_session:
            _close_session(sid)


def _upsert_row_impl(sheet_name, key_col_index, key_value, values_list, num_cols, ext_session=None):
    """
    Find row by key column → update it. If not found → append.
    Returns 'updated' or 'appended'.
    """
    own_session = ext_session is None
    sid, h = _open_session() if own_session else ext_session
    try:
        url = f"{_ws_url(sheet_name)}/usedRange"
        data = _req('GET', url, h)
        all_values = data.get('values', [])

        needle = str(key_value).strip().upper()
        target_row = None
        for i in range(1, len(all_values)):
            row = all_values[i]
            if key_col_index < len(row):
                cell = _normalise_cell(row[key_col_index])
                if cell == needle:
                    target_row = i + 1  # 1-based
                    break

        if target_row:
            addr = f"A{target_row}:{_col_letter(num_cols)}{target_row}"
            write_url = f"{_ws_url(sheet_name)}/range(address='{addr}')"
            _req('PATCH', write_url, h, json_data={"values": [_safe_row(values_list)]})
            logger.info(f"Upsert: updated row {target_row} in '{sheet_name}'")
            return 'updated'
        else:
            next_row = len(all_values) + 1
            addr = f"A{next_row}:{_col_letter(num_cols)}{next_row}"
            write_url = f"{_ws_url(sheet_name)}/range(address='{addr}')"
            _req('PATCH', write_url, h, json_data={"values": [_safe_row(values_list)]})
            logger.info(f"Upsert: appended row {next_row} to '{sheet_name}'")
            return 'appended'
    finally:
        if own_session:
            _close_session(sid)


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC API  — unchanged signatures, own-session wrappers
#
#  All existing callers (admin panel, health-check, duplicate check, offline
#  sync, etc.) continue to work exactly as before — each call manages its own
#  single session with no changes required at the call site.
# ═══════════════════════════════════════════════════════════════════════════════

def read_all_rows(sheet_name):
    """
    Read entire used range from a worksheet.
    Returns (headers: list[str], rows: list[list]).
    Row 1 = headers. Row 2+ = data.
    """
    return _read_all_rows_impl(sheet_name, ext_session=None)


def read_all_rows_as_dicts(sheet_name):
    """Read sheet → list of dicts keyed by header names."""
    headers, rows = read_all_rows(sheet_name)
    result = []
    for row in rows:
        d = {}
        for i, hdr in enumerate(headers):
            d[hdr] = row[i] if i < len(row) else None
        result.append(d)
    return result


def read_all_rows_from_file(filename, sheet_name):
    """
    Read entire used range from a worksheet in ANY workbook in the same
    SharePoint drive — not just WORKBOOK_FILENAME.

    Uses the proven /drive/root:/{filename}:/workbook pattern with its own
    session (opened and closed internally).

    Returns (headers: list[str], rows: list[list]).
    Row 1 = headers. Row 2+ = data.
    """
    sid, h = _open_session_for(filename)
    try:
        url = f"{_ws_url_for(filename, sheet_name)}/usedRange"
        data = _req('GET', url, h)
        values = data.get('values', [])
        if not values:
            return [], []
        headers = [str(v) if v is not None else '' for v in values[0]]
        rows = values[1:] if len(values) > 1 else []
        logger.info(f"Read {len(rows)} rows from '{sheet_name}' in '{filename}'")
        return headers, rows
    finally:
        _close_session_for(filename, sid)


def find_row(sheet_name, col_index, search_value):
    """
    Find first data row where col_index matches search_value (case-insensitive).
    col_index is 0-based (A=0, B=1, ...).
    Returns (row_number_1based, row_values_list) or (None, None).
    """
    sid, h = _open_session()
    try:
        url = f"{_ws_url(sheet_name)}/usedRange"
        data = _req('GET', url, h)
        values = data.get('values', [])
        if len(values) < 2:
            return None, None
        needle = str(search_value).strip().upper()
        for i in range(1, len(values)):
            row = values[i]
            if col_index < len(row):
                cell = _normalise_cell(row[col_index])
                if cell == needle:
                    return i + 1, row
        return None, None
    finally:
        _close_session(sid)


def find_row_as_dict(sheet_name, col_name, search_value):
    """
    Find first row where column named col_name matches search_value.
    Returns dict or None.
    """
    return _find_row_as_dict_impl(sheet_name, col_name, search_value, ext_session=None)


def append_row(sheet_name, values_list, num_cols):
    """
    Append a single data row after the last REAL data row.
    Determines next row from column A only (all sheets have a key in col A).
    """
    return _append_row_impl(sheet_name, values_list, num_cols, ext_session=None)


def update_row(sheet_name, row_number, values_list, num_cols):
    """
    Overwrite a specific row (1-based row_number).
    """
    sid, h = _open_session()
    try:
        addr = f"A{row_number}:{_col_letter(num_cols)}{row_number}"
        url = f"{_ws_url(sheet_name)}/range(address='{addr}')"
        _req('PATCH', url, h, json_data={"values": [_safe_row(values_list)]})
        logger.info(f"Updated row {row_number} in '{sheet_name}'")
    finally:
        _close_session(sid)


def upsert_row(sheet_name, key_col_index, key_value, values_list, num_cols):
    """
    Find row by key column → update it. If not found → append.
    Returns 'updated' or 'appended'.
    """
    return _upsert_row_impl(sheet_name, key_col_index, key_value, values_list, num_cols, ext_session=None)


def test_connection():
    """Test connectivity. Returns status dict with list of worksheets."""
    try:
        sid, h = _open_session()
        try:
            url = f"{_wb_url()}/worksheets"
            data = _req('GET', url, h)
            sheets = [s['name'] for s in data.get('value', [])]
            return {"status": "connected", "sheets": sheets}
        finally:
            _close_session(sid)
    except GraphAPIError as e:
        return {"status": "error", "message": e.message, "details": e.details}
    except Exception as e:
        return {"status": "error", "message": str(e)}
