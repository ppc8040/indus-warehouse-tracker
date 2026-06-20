"""
SharePoint Master Data — reads 'CargoLoad Pro Master.xlsx' → 'Indus FG' sheet
directly from SharePoint via graph_client.read_all_rows_from_file().

This replaces the indirect cargoload_client.fetch_cargo_items() REST hop so
the Tracker reads dimensions from the same source as CargoLoad Pro, with no
dependency on CargoLoad Pro being up.

Cached in-memory for 5 minutes (same TTL as OOR cache).

Public API:
    fetch_dimensions() -> dict[str, dict]
        Returns upper-cased Indus Part -> row dict with keys:
            'Length (mm)', 'Width (mm)', 'Height (mm)', 'Weight per coil (KG)'
"""
import time
import logging

logger = logging.getLogger(__name__)

_CARGOLOAD_WORKBOOK = 'CargoLoad Pro Master.xlsx'
_INDUS_FG_SHEET     = 'Indus FG'

_dims_cache = {'data': None, 'ts': 0.0}
_DIMS_CACHE_TTL = 300  # seconds


def fetch_dimensions() -> dict:
    """
    Returns a dict keyed by UPPER(Indus Part) -> row dict from 'Indus FG' sheet.
    Cached for 5 minutes. Returns {} on any failure (never crashes the caller).
    """
    now = time.time()
    if _dims_cache['data'] is not None and (now - _dims_cache['ts']) < _DIMS_CACHE_TTL:
        return _dims_cache['data']

    try:
        from app.services.graph_client import read_all_rows_from_file
        headers, rows = read_all_rows_from_file(_CARGOLOAD_WORKBOOK, _INDUS_FG_SHEET)

        if not headers or not rows:
            logger.warning('sharepoint_master: Indus FG sheet returned no data')
            return _dims_cache['data'] or {}

        result = {}
        for row in rows:
            row_dict = {headers[i]: (row[i] if i < len(row) else None) for i in range(len(headers))}
            key = str(row_dict.get('Indus Part') or '').strip().upper()
            if key and key not in ('', 'NONE', 'N/A', '#N/A'):
                result[key] = row_dict

        _dims_cache['data'] = result
        _dims_cache['ts']   = now
        logger.info('sharepoint_master: loaded %d parts from Indus FG', len(result))
        return result

    except Exception as exc:
        logger.error('sharepoint_master: fetch_dimensions failed: %s', exc)
        return _dims_cache['data'] or {}
