"""
Atomic warehouse loading service.

Policy:
- Existing HUs are eligible only when their current state is STOCK_IN.
- Existing HUs in any other state are blocked. Supervisor override does not
  overwrite their history.
- A genuinely missing HU may be posted through a traceable fallback:
  STOCK_IN followed immediately by STOCK_OUT to LOADING.
- Missing-HU fallback requires a server-validated supervisor approval.
- All database writes for a load occur in one transaction.
- Repeating the same idempotency key returns the original completed loading.
"""

import logging
import uuid
from datetime import datetime, timezone

from flask import current_app

from app.services.db_client import DbError, db_transaction
from app.utils.validators import (
    sanitize_text,
    validate_customer_part,
    validate_handling_unit,
    validate_indus_part,
    validate_pallet_qty,
    validate_supervisor_pin,
)

logger = logging.getLogger(__name__)

VALID_SOURCE_LOCATIONS = {"M4", "M5", "M6", "M4_STORE", "M16_STORE"}
DESTINATION = "LOADING"
MAX_LOADING_ITEMS = 250


class LoadingResult:
    def __init__(self, success: bool, status: str, message: str, data: dict = None):
        self.success = success
        self.status = status
        self.message = message
        self.data = data or {}

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "status": self.status,
            "message": self.message,
            "data": self.data,
        }


def _text(value, max_len=500) -> str:
    return sanitize_text(value or "", max_len=max_len)


def _new_event_id() -> str:
    return str(uuid.uuid4())[:12].upper()


def _normalise_location(value) -> str:
    return _text(value, 30).upper()


def _normalise_items(raw_items) -> tuple[list, list]:
    if not isinstance(raw_items, list):
        return [], ["Items must be an array."]

    if not raw_items:
        return [], ["Add at least one handling unit before posting."]

    if len(raw_items) > MAX_LOADING_ITEMS:
        return [], [f"A loading cannot contain more than {MAX_LOADING_ITEMS} HUs."]

    clean_items = []
    errors = []
    seen_hus = set()

    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            errors.append(f"Item {index}: invalid item format.")
            continue

        ok_hu, hu_or_message = validate_handling_unit(raw.get("handling_unit", ""))
        if not ok_hu:
            errors.append(f"Item {index}: {hu_or_message}")
            continue

        hu = hu_or_message
        if hu in seen_hus:
            errors.append(f"Item {index}: HU {hu} appears more than once in this loading.")
            continue
        seen_hus.add(hu)

        ok_part, part_or_message = validate_indus_part(raw.get("indus_part", ""))
        if not ok_part:
            errors.append(f"Item {index} / HU {hu}: {part_or_message}")
            continue

        ok_customer, customer_or_message = validate_customer_part(
            raw.get("customer_part", ""),
            part_or_message,
        )
        if not ok_customer:
            errors.append(f"Item {index} / HU {hu}: {customer_or_message}")
            continue

        ok_qty, qty_or_message = validate_pallet_qty(raw.get("pallet_qty", ""))
        if not ok_qty:
            errors.append(f"Item {index} / HU {hu}: {qty_or_message}")
            continue

        if not qty_or_message:
            errors.append(f"Item {index} / HU {hu}: Quantity is required for loading.")
            continue

        clean_items.append(
            {
                "sequence_no": index,
                "handling_unit": hu,
                "indus_part": part_or_message,
                "customer_part": customer_or_message,
                "pallet_qty": int(qty_or_message),
            }
        )

    return clean_items, errors


def _validate_request(payload: dict) -> tuple[dict | None, list]:
    payload = payload if isinstance(payload, dict) else {}

    idempotency_key = _text(payload.get("idempotency_key", ""), max_len=120)
    source_location = _normalise_location(payload.get("source_location", ""))
    operator_id = _text(payload.get("operator_id", "WORKER"), max_len=100) or "WORKER"
    device_id = _text(payload.get("device_id", ""), max_len=200)
    shipment_reference = _text(payload.get("shipment_reference", ""),max_len=200,)
    override = payload.get("supervisor_override") or {}

    errors = []

    if not idempotency_key:
        errors.append("A loading idempotency key is required.")

    if source_location not in VALID_SOURCE_LOCATIONS:
        errors.append(
            "Source location is required. Select M4, M5, M6, M4_STORE, or M16_STORE."
        )

    items, item_errors = _normalise_items(payload.get("items"))
    errors.extend(item_errors)

    return (
      {
          "idempotency_key": idempotency_key,
          "source_location": source_location,
          "operator_id": operator_id,
          "device_id": device_id,
          "shipment_reference": shipment_reference,
          "items": items,
          "supervisor_approved": bool(override.get("approved")),
          "supervisor_pin": _text(override.get("pin", ""), max_len=100),
          "override_reason": _text(override.get("reason", ""), max_len=500),
      },
        errors,
    )


def _load_existing_header(cursor, idempotency_key: str) -> dict | None:
    cursor.execute(
        """
        SELECT
            loading_id,
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
        FOR UPDATE
        """,
        (idempotency_key,),
    )
    row = cursor.fetchone()
    if not row:
        return None

    columns = [column.name for column in cursor.description]
    return dict(zip(columns, row))


def _fetch_status_for_update(cursor, handling_unit: str) -> dict | None:
    cursor.execute(
        """
        SELECT
            handling_unit,
            current_status,
            last_event_id,
            last_movement_type,
            last_movement_time,
            indus_part,
            pallet_qty,
            last_location,
            hu_creation_date,
            hu_batch
        FROM pallet_status
        WHERE handling_unit = %s
        FOR UPDATE
        """,
        (handling_unit,),
    )

    row = cursor.fetchone()
    return dict(row) if row else None


def _write_movement(
    cursor,
    *,
    event_id: str,
    timestamp_utc: str,
    handling_unit: str,
    movement_type: str,
    indus_part: str,
    customer_part: str,
    pallet_qty: int,
    operator_id: str,
    device_id: str,
    location: str,
    from_location: str,
    to_location: str,
    validation_status: str,
    loading_id: str,
    loading_item_id: str,
):
    cursor.execute(
        """
        INSERT INTO movements (
            event_id,
            timestamp_utc,
            handling_unit,
            movement_type,
            indus_part,
            customer_part,
            pallet_qty,
            job_no,
            operator_id,
            device_id,
            sync_status,
            validation_status,
            location,
            from_location,
            to_location,
            loading_id,
            loading_item_id
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s
        )
        """,
        (
            event_id,
            timestamp_utc,
            handling_unit,
            movement_type,
            indus_part,
            customer_part,
            pallet_qty,
            "",
            operator_id,
            device_id,
            "SYNCED",
            validation_status,
            location,
            from_location,
            to_location,
            loading_id,
            loading_item_id,
        ),
    )


def _upsert_pallet_status(
    cursor,
    *,
    handling_unit: str,
    current_status: str,
    event_id: str,
    movement_type: str,
    timestamp_utc: str,
    indus_part: str,
    pallet_qty: int,
    location: str,
):
    cursor.execute(
        """
        INSERT INTO pallet_status (
            handling_unit,
            current_status,
            last_event_id,
            last_movement_type,
            last_movement_time,
            indus_part,
            pallet_qty,
            last_location
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (handling_unit)
        DO UPDATE SET
            current_status = EXCLUDED.current_status,
            last_event_id = EXCLUDED.last_event_id,
            last_movement_type = EXCLUDED.last_movement_type,
            last_movement_time = EXCLUDED.last_movement_time,
            indus_part = EXCLUDED.indus_part,
            pallet_qty = EXCLUDED.pallet_qty,
            last_location = EXCLUDED.last_location
        """,
        (
            handling_unit,
            current_status,
            event_id,
            movement_type,
            timestamp_utc,
            indus_part,
            pallet_qty,
            location,
        ),
    )


def review_loading(payload: dict) -> LoadingResult:
    """
    Read-only server review. It never creates records.

    The final posting endpoint repeats every important validation inside
    its transaction because review data can become stale.
    """
    request, errors = _validate_request(payload)
    if errors:
        return LoadingResult(
            False,
            "rejected_validation",
            "Review failed.",
            {"errors": errors},
        )

    results = []
    missing_count = 0

    try:
        with db_transaction() as connection:
            with connection.cursor() as cursor:
                for item in request["items"]:
                    current = _fetch_status_for_update(cursor, item["handling_unit"])

                    if current is None:
                        missing_count += 1
                        results.append(
                            {
                                **item,
                                "eligible": request["supervisor_approved"],
                                "resolution": "MISSING_HU_FALLBACK",
                                "message": (
                                    "HU is not in warehouse records. Supervisor approval "
                                    "is required for traceable STOCK_IN then STOCK_OUT."
                                ),
                            }
                        )
                        continue

                    current_type = _text(current.get("last_movement_type", "")).upper()
                    current_location = _normalise_location(current.get("last_location", ""))

                    if current_type != "STOCK_IN":
                        results.append(
                            {
                                **item,
                                "eligible": False,
                                "resolution": "BLOCKED_EXISTING_NOT_STOCK_IN",
                                "message": (
                                    f"HU is currently {current_type or 'unknown'}, not STOCK_IN. "
                                    "Existing history will not be overwritten."
                                ),
                            }
                        )
                        continue

                    if current_location != request["source_location"]:
                        results.append(
                            {
                                **item,
                                "eligible": False,
                                "resolution": "BLOCKED_LOCATION_MISMATCH",
                                "message": (
                                    f"HU is stocked at {current_location or 'an unknown location'}, "
                                    f"not {request['source_location']}."
                                ),
                            }
                        )
                        continue

                    if current.get("indus_part") and current["indus_part"] != item["indus_part"]:
                        results.append(
                            {
                                **item,
                                "eligible": False,
                                "resolution": "BLOCKED_PART_MISMATCH",
                                "message": "Submitted Indus Part does not match the warehouse record.",
                            }
                        )
                        continue

                    if current.get("pallet_qty") and int(current["pallet_qty"]) != item["pallet_qty"]:
                        results.append(
                            {
                                **item,
                                "eligible": False,
                                "resolution": "BLOCKED_QTY_MISMATCH",
                                "message": "Submitted quantity does not match the warehouse record.",
                            }
                        )
                        continue

                    results.append(
                        {
                            **item,
                            "eligible": True,
                            "resolution": "EXISTING_STOCK_IN",
                            "message": "Eligible for STOCK_OUT to LOADING.",
                        }
                    )
    except Exception as exc:
        logger.exception("Loading review failed")
        return LoadingResult(
            False,
            "failed_review",
            "Could not review this loading. Please try again.",
            {"errors": [str(exc)]},
        )

    blocked = [
        row
        for row in results
        if not row["eligible"]
        and row["resolution"] != "MISSING_HU_FALLBACK"
    ]
    needs_approval = missing_count > 0 and not request["supervisor_approved"]

    return LoadingResult(
        not blocked and not needs_approval,
        "review_ready" if not blocked and not needs_approval else "review_blocked",
        "Review complete." if not blocked and not needs_approval else "Review requires action.",
        {
            "source_location": request["source_location"],
            "destination": DESTINATION,
            "items": results,
            "missing_count": missing_count,
            "blocked_count": len(blocked),
            "requires_supervisor_approval": missing_count > 0,
        },
    )


def post_loading(payload: dict) -> LoadingResult:
    request, errors = _validate_request(payload)
    if errors:
        return LoadingResult(
            False,
            "rejected_validation",
            "Loading validation failed.",
            {"errors": errors},
        )

    now = datetime.now(timezone.utc).isoformat()
    loading_id = str(uuid.uuid4())

    try:
        with db_transaction() as connection:
            with connection.cursor() as cursor:
                existing = _load_existing_header(cursor, request["idempotency_key"])

                if existing:
                    if existing["status"] == "COMPLETED":
                        return LoadingResult(
                            True,
                            "completed",
                            "This loading was already posted.",
                            {
                                "loading_id": str(existing["loading_id"]),
                                "replayed": True,
                                "item_count": existing["item_count"],
                            },
                        )

                    return LoadingResult(
                        False,
                        "posting_in_progress",
                        "This loading is already being processed. Please wait and refresh.",
                        {"loading_id": str(existing["loading_id"])},
                    )

                  cursor.execute(
                      """
                      INSERT INTO loading_headers (
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
                          created_at
                      )
                      VALUES (
                          %s, %s, 'POSTING', %s, %s, %s, %s, %s, %s, %s, %s, %s
                      )
                      """,
                      (
                          loading_id,
                          request["idempotency_key"],
                          request["source_location"],
                          DESTINATION,
                          request["operator_id"],
                          request["device_id"],
                          request["shipment_reference"],
                          request["supervisor_approved"],
                          request["override_reason"],
                          len(request["items"]),
                          now,
                      ),
                  )

                planned = []
                missing_items = []

                for item in request["items"]:
                    current = _fetch_status_for_update(cursor, item["handling_unit"])

                    if current is None:
                        missing_items.append(item)
                        planned.append(
                            {
                                **item,
                                "resolution": "MISSING_HU_FALLBACK",
                                "current": None,
                            }
                        )
                        continue

                    current_type = _text(current.get("last_movement_type", "")).upper()
                    current_location = _normalise_location(current.get("last_location", ""))

                    if current_type != "STOCK_IN":
                        raise ValueError(
                            f"HU {item['handling_unit']} is {current_type or 'unknown'}, "
                            "not STOCK_IN. Existing HU history cannot be overwritten."
                        )

                    if current_location != request["source_location"]:
                        raise ValueError(
                            f"HU {item['handling_unit']} is stocked at "
                            f"{current_location or 'an unknown location'}, not "
                            f"{request['source_location']}."
                        )

                    if current.get("indus_part") and current["indus_part"] != item["indus_part"]:
                        raise ValueError(
                            f"HU {item['handling_unit']} has an Indus Part mismatch."
                        )

                    if current.get("pallet_qty") and int(current["pallet_qty"]) != item["pallet_qty"]:
                        raise ValueError(
                            f"HU {item['handling_unit']} has a quantity mismatch."
                        )

                    planned.append(
                        {
                            **item,
                            "resolution": "EXISTING_STOCK_IN",
                            "current": current,
                        }
                    )

                if missing_items:
                    configured_pin = current_app.config.get("SUPERVISOR_PIN", "")

                    if not request["supervisor_approved"]:
                        raise ValueError(
                            "Supervisor approval is required because one or more HUs "
                            "are missing from warehouse records."
                        )

                    if not validate_supervisor_pin(request["supervisor_pin"], configured_pin):
                        raise ValueError("Supervisor approval could not be verified.")

                    if not request["override_reason"]:
                        raise ValueError(
                            "A supervisor override reason is required for missing HUs."
                        )

                created_items = []

                for item in planned:
                    loading_item_id = str(uuid.uuid4())
                    stock_in_event_id = None
                    stock_out_event_id = _new_event_id()

                    if item["resolution"] == "MISSING_HU_FALLBACK":
                        stock_in_event_id = _new_event_id()

                        _write_movement(
                            cursor,
                            event_id=stock_in_event_id,
                            timestamp_utc=now,
                            handling_unit=item["handling_unit"],
                            movement_type="STOCK_IN",
                            indus_part=item["indus_part"],
                            customer_part=item["customer_part"],
                            pallet_qty=item["pallet_qty"],
                            operator_id=request["operator_id"],
                            device_id=request["device_id"],
                            location=request["source_location"],
                            from_location="",
                            to_location="",
                            validation_status="OVERRIDE",
                            loading_id=loading_id,
                            loading_item_id=loading_item_id,
                        )

                    _write_movement(
                        cursor,
                        event_id=stock_out_event_id,
                        timestamp_utc=now,
                        handling_unit=item["handling_unit"],
                        movement_type="STOCK_OUT",
                        indus_part=item["indus_part"],
                        customer_part=item["customer_part"],
                        pallet_qty=item["pallet_qty"],
                        operator_id=request["operator_id"],
                        device_id=request["device_id"],
                        location=request["source_location"],
                        from_location=request["source_location"],
                        to_location=DESTINATION,
                        validation_status=(
                            "OVERRIDE"
                            if item["resolution"] == "MISSING_HU_FALLBACK"
                            else "VALID"
                        ),
                        loading_id=loading_id,
                        loading_item_id=loading_item_id,
                    )

                    _upsert_pallet_status(
                        cursor,
                        handling_unit=item["handling_unit"],
                        current_status="OUT_OF_WAREHOUSE",
                        event_id=stock_out_event_id,
                        movement_type="STOCK_OUT",
                        timestamp_utc=now,
                        indus_part=item["indus_part"],
                        pallet_qty=item["pallet_qty"],
                        location=request["source_location"],
                    )

                    cursor.execute(
                        """
                        INSERT INTO loading_items (
                            loading_item_id,
                            loading_id,
                            handling_unit,
                            sequence_no,
                            resolution,
                            stock_in_event_id,
                            stock_out_event_id,
                            indus_part,
                            customer_part,
                            pallet_qty,
                            created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            loading_item_id,
                            loading_id,
                            item["handling_unit"],
                            item["sequence_no"],
                            item["resolution"],
                            stock_in_event_id,
                            stock_out_event_id,
                            item["indus_part"],
                            item["customer_part"],
                            item["pallet_qty"],
                            now,
                        ),
                    )

                    created_items.append(
                        {
                            "handling_unit": item["handling_unit"],
                            "resolution": item["resolution"],
                            "stock_in_event_id": stock_in_event_id,
                            "stock_out_event_id": stock_out_event_id,
                        }
                    )

                cursor.execute(
                    """
                    UPDATE loading_headers
                    SET status = 'COMPLETED',
                        completed_at = %s
                    WHERE loading_id = %s
                    """,
                    (now, loading_id),
                )

        return LoadingResult(
            True,
            "completed",
            f"Loading posted: {len(request['items'])} HU(s).",
            {
                "loading_id": loading_id,
                "replayed": False,
                "item_count": len(request["items"]),
                "items": created_items,
            },
        )

    except ValueError as exc:
        return LoadingResult(
            False,
            "rejected_validation",
            str(exc),
            {"errors": [str(exc)]},
        )
    except DbError as exc:
        logger.exception("Loading database failure")
        return LoadingResult(
            False,
            "failed_write_logged",
            "Could not save this loading. Nothing was posted. Please retry.",
            {"errors": [str(exc)]},
        )
    except Exception as exc:
        logger.exception("Loading posting failure")
        return LoadingResult(
            False,
            "failed_write_logged",
            "Could not save this loading. Nothing was posted. Please retry.",
            {"errors": [str(exc)]},
        )
