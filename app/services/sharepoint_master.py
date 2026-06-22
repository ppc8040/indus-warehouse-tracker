"""
SharePoint Master Data — reads 'CargoLoad Pro Master.xlsx' → 'Indus FG' sheet
Uses search-by-filename (same pattern as OOR) instead of path-based session
to avoid Graph API 503 errors on filenames with spaces.
"""
import time
import logging
import requests as _req_lib

logger = logging.getLogger(__name__)

_CARGOLOAD_WORKBOOK = 'CargoLoad Pro Master.xlsx'
_INDUS_FG_SHEET     = 'Indus FG'

_dims_cache = {'data': None, 'ts': 0.0}
_DIMS_CACHE_TTL = 300


def fetch_dimensions() -> dict:
    """
    Returns dict keyed by UPPER(Indus Part) -> row dict from 'Indus FG' sheet.
    Uses search-by-filename to locate the file, then reads via item ID.
    Cached 5 minutes. Returns {} on failure.
    """
    now = time.time()
    if _dims_cache['data'] is not None and (now - _dims_cache['ts']) < _DIMS_CACHE_TTL:
        return _dims_cache['data']

    try:
        from flask import current_app
        cfg     = current_app.config
        tenant  = cfg['GRAPH_TENANT_ID']
        cid     = cfg['GRAPH_CLIENT_ID']
        secret  = cfg['GRAPH_CLIENT_SECRET']
        site_id = cfg['SHAREPOINT_SITE_ID']
        timeout = cfg.get('GRAPH_REQUEST_TIMEOUT', 30)

        # 1. Get token
        token_resp = _req_lib.post(
            f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token',
            data={'grant_type': 'client_credentials', 'client_id': cid,
                  'client_secret': secret, 'scope': 'https://graph.microsoft.com/.default'},
            timeout=timeout
        )
        token_resp.raise_for_status()
        token = token_resp.json()['access_token']
        headers = {'Authorization': f'Bearer {token}'}

        # 2. Search for the file by name (same as OOR — avoids path encoding issues)
        search_resp = _req_lib.get(
            f'https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root/search(q=\'{_CARGOLOAD_WORKBOOK}\')',
            headers=headers, timeout=timeout
        )
        search_resp.raise_for_status()
        items = search_resp.json().get('value', [])
        item = next((i for i in items if i.get('name', '').lower() == _CARGOLOAD_WORKBOOK.lower()), None)
        if not item:
            logger.warning('sharepoint_master: %s not found in SharePoint', _CARGOLOAD_WORKBOOK)
            return _dims_cache['data'] or {}

        item_id = item['id']

        # 3. Read the Indus FG sheet used range
        range_resp = _req_lib.get(
            f'https://graph.microsoft.com/v1.0/sites/{site_id}/drive/items/{item_id}'
            f'/workbook/worksheets(\'{_INDUS_FG_SHEET}\')/usedRange',
            headers=headers, timeout=60
        )
        range_resp.raise_for_status()
        all_rows = range_resp.json().get('values', [])

        if len(all_rows) < 2:
            logger.warning('sharepoint_master: Indus FG sheet returned no data')
            return _dims_cache['data'] or {}

        col_headers = [str(v).strip() if v is not None else '' for v in all_rows[0]]
        result = {}
        for row in all_rows[1:]:
            row_dict = {col_headers[i]: (row[i] if i < len(row) else None) for i in range(len(col_headers))}
            key = str(row_dict.get('Indus Part') or '').strip().upper()
            if key and key not in ('', 'NONE', 'N/A', '#N/A'):
                result[key] = row_dict

        _dims_cache['data'] = result
        _dims_cache['ts'] = now
        logger.info('sharepoint_master: loaded %d parts from Indus FG', len(result))
        return result

    except Exception as exc:
        logger.error('sharepoint_master: fetch_dimensions failed: %s', exc)
        return _dims_cache['data'] or {}
