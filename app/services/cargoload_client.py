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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

_TIMEOUT = 60  # increased from 30


def _session():
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=1, status_forcelist=[502, 503, 504])
    s.mount('https://', HTTPAdapter(max_retries=retry))
    return s


def _headers(cfg):
    return {
        'X-Internal-Key': cfg.get('INTERLINK_API_KEY', ''),
        'Content-Type': 'application/json',
    }


def fetch_cargo_items(cfg):
    url = cfg.get('CARGOLOAD_API_URL', '').rstrip('/') + '/sharepoint/cargo-items'
    try:
        resp = _session().get(url, headers=_headers(cfg), timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error('cargoload fetch_cargo_items failed: %s', exc)
        return None


def pack_single_sku(cfg, payload: dict):
    url = cfg.get('CARGOLOAD_API_URL', '').rstrip('/') + '/pack/single-sku'
    try:
        resp = _session().post(url, json=payload, headers=_headers(cfg), timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error('cargoload pack_single_sku failed for %s: %s', payload.get('indus_part'), exc)
        return None
