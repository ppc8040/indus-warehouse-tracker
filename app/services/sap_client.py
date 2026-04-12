"""
SAP S/4HANA Public Cloud — Handling Unit lookup.

Calls the OData4 HandlingUnitItem API to retrieve Material and Quantity
for a given Handling Unit External ID.

Credentials are hardcoded here (same pattern as config.py).
API returns OData4 JSON; we take the first matching item per HU
(one HU always has one material in this system).

Fields used:
  HandlingUnitExternalID  → handling_unit
  Material                → indus_part
  HandlingUnitQuantity    → pallet_qty (integer, stored as string)

error_code values returned inside SAPLookupError:
  HU_NOT_FOUND   — API returned 0 items for this HU (HU exists in warehouse but not in SAP)
  HU_NO_MATERIAL — SAP record has no Material field
  HU_ZERO_QTY    — SAP record has zero or invalid quantity
  SAP_AUTH       — 401/403 from SAP
  SAP_TIMEOUT    — request timed out
  SAP_CONN       — connection error (network unreachable)
  SAP_HTTP       — non-2xx response other than auth errors
  SAP_PARSE      — non-JSON or unexpected response body
  SAP_UNKNOWN    — any other unexpected exception
"""
import logging
import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

# ── SAP API credentials (hardcoded, no env dependency) ──
_SAP_BASE_URL = (
    "https://my409486-api.s4hana.cloud.sap"
    "/sap/opu/odata4/sap/api_handlingunit/srvd_a2x/sap/handlingunit/0001"
)
_SAP_USERNAME = "INDUS_OPEN_ORDER"
_SAP_PASSWORD = "w6fo3zfq/bDYr}8>BU@{i3oFd[dq#Zv\\/eL&}SKF"

_REQUEST_TIMEOUT = 20  # seconds


class SAPLookupError(Exception):
    """
    Raised when the SAP API call fails or returns no data.
    Carries an error_code string so the caller can differentiate
    between "HU not found" and other failure types.
    """
    def __init__(self, message: str, error_code: str = "SAP_UNKNOWN"):
        super().__init__(message)
        self.error_code = error_code


def lookup_hu(handling_unit: str) -> dict:
    """
    Look up a Handling Unit in SAP and return its material and quantity.

    Args:
        handling_unit: The 18-20 digit HU number (already validated).

    Returns:
        dict with keys:
          - handling_unit  (str)  — the HU number as returned by SAP
          - indus_part     (str)  — the Material field (e.g. 'AJM0488')
          - pallet_qty     (str)  — HandlingUnitQuantity as a plain integer string (e.g. '36')

    Raises:
        SAPLookupError: if the API is unreachable, returns an error,
                        or returns no items for the given HU.
                        Check .error_code for the specific failure type.
    """
    url = f"{_SAP_BASE_URL}/HandlingUnitItem"
    params = {
        "$filter": f"HandlingUnitExternalID eq '{handling_unit}'",
        "$select": "HandlingUnitExternalID,Material,HandlingUnitQuantity",
        "$top": "1",
    }
    headers = {
        "Accept": "application/json",
    }

    try:
        resp = requests.get(
            url,
            params=params,
            headers=headers,
            auth=HTTPBasicAuth(_SAP_USERNAME, _SAP_PASSWORD),
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        logger.error("SAP API timeout for HU %s", handling_unit)
        raise SAPLookupError(
            "SAP system did not respond in time. Check network and try again.",
            error_code="SAP_TIMEOUT",
        )
    except requests.exceptions.ConnectionError as e:
        logger.error("SAP API connection error for HU %s: %s", handling_unit, e)
        raise SAPLookupError(
            "Cannot reach SAP system. Check your network connection and try again.",
            error_code="SAP_CONN",
        )
    except Exception as e:
        logger.error("SAP API unexpected error for HU %s: %s", handling_unit, e)
        raise SAPLookupError(
            f"SAP lookup failed unexpectedly: {e}",
            error_code="SAP_UNKNOWN",
        )

    if resp.status_code in (401, 403):
        logger.error("SAP API auth error (%s) for HU %s", resp.status_code, handling_unit)
        raise SAPLookupError(
            "SAP authentication failed. Contact administrator.",
            error_code="SAP_AUTH",
        )

    if not resp.ok:
        logger.error(
            "SAP API HTTP %s for HU %s: %s",
            resp.status_code, handling_unit, resp.text[:200]
        )
        raise SAPLookupError(
            f"SAP returned an error (HTTP {resp.status_code}). Try again.",
            error_code="SAP_HTTP",
        )

    try:
        data = resp.json()
    except Exception:
        logger.error("SAP API returned non-JSON for HU %s", handling_unit)
        raise SAPLookupError(
            "SAP returned an unexpected response. Try again.",
            error_code="SAP_PARSE",
        )

    # OData4 wraps results in {"value": [...]}
    items = data.get("value", [])
    if not items:
        # This is the specific case where the HU physically exists in the
        # warehouse but is not (or no longer) active in SAP HandlingUnitItem.
        # The frontend uses error_code=HU_NOT_FOUND to trigger the supervisor
        # override path instead of a plain error toast.
        logger.warning("SAP returned no items for HU %s", handling_unit)
        raise SAPLookupError(
            f"Handling Unit {handling_unit} was not found in SAP. "
            "If this pallet is physically in the warehouse, use supervisor override.",
            error_code="HU_NOT_FOUND",
        )

    item = items[0]

    material = str(item.get("Material", "")).strip().upper()
    if not material:
        logger.error("SAP item for HU %s has no Material field", handling_unit)
        raise SAPLookupError(
            f"SAP record for HU {handling_unit} has no material. Contact administrator.",
            error_code="HU_NO_MATERIAL",
        )

    # HandlingUnitQuantity is a number in the API; convert to plain integer string
    raw_qty = item.get("HandlingUnitQuantity", 0)
    try:
        qty_int = int(float(str(raw_qty)))
    except (ValueError, TypeError):
        qty_int = 0

    if qty_int < 1:
        logger.warning(
            "SAP item for HU %s has zero/invalid quantity: %s", handling_unit, raw_qty
        )
        raise SAPLookupError(
            f"SAP record for HU {handling_unit} has invalid quantity ({raw_qty}). "
            "Contact administrator.",
            error_code="HU_ZERO_QTY",
        )

    hu_from_sap = str(item.get("HandlingUnitExternalID", handling_unit)).strip()

    logger.info(
        "SAP lookup OK: HU=%s material=%s qty=%s",
        hu_from_sap, material, qty_int
    )

    return {
        "handling_unit": hu_from_sap,
        "indus_part": material,
        "pallet_qty": str(qty_int),
    }
