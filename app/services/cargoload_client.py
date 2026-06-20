"""
CargoLoad Pro integration client.

Two functions consumed by admin.py:
  - fetch_cargo_items()   → GET /sharepoint/cargo-items  (dimensions catalogue)
  - pack_single_sku(payload) → POST /pack/single-sku     (packing simulation)

Both require INTERLINK_API_KEY in config. On any error they return None
so a CargoLoad outage never crashes the Tracker dashboard.
"""
import logging
import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 30


def _headers(cfg):
    return {
        'X-Internal-Key': cfg.get('INTERLINK_API_KEY', ''),
        'Content-Type': 'application/json',
    }


def fetch_cargo_items(cfg):
    """
    Returns list of cargo item dicts from CargoLoad Pro's SharePoint catalogue,
    or None on failure.
    """
    url = cfg.get('CARGOLOAD_API_URL', '').rstrip('/') + '/sharepoint/cargo-items'
    try:
        resp = requests.get(url, headers=_headers(cfg), timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error('cargoload fetch_cargo_items failed: %s', exc)
        return None


def pack_single_sku(cfg, payload: dict):
    """
    Calls POST /pack/single-sku on CargoLoad Pro.
    payload must contain: indus_part, length, width, height, weight,
                          stackable, rotatable, floor_only, container_id
    Returns dict with items_placed / items_not_placed, or None on failure.
    """
    url = cfg.get('CARGOLOAD_API_URL', '').rstrip('/') + '/pack/single-sku'
    try:
        resp = requests.post(url, json=payload, headers=_headers(cfg), timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error('cargoload pack_single_sku failed for %s: %s', payload.get('indus_part'), exc)
        return None
