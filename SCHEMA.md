# PostgreSQL Schema — Indus Warehouse Tracker

All data lives in **Heroku Postgres**. Schema is created and maintained by the scripts in `scripts/`.

> **Note:** This project previously used a Microsoft SharePoint Excel workbook as its backend (accessed via Microsoft Graph API). That backend was fully replaced by PostgreSQL in April 2026. The Excel schema is no longer relevant.

---

## Tables

### `movements`
Every STOCK_IN, STOCK_OUT, and ERROR event posted by warehouse operators.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `BIGSERIAL` | Primary key |
| `event_id` | `TEXT UNIQUE NOT NULL` | UUID per movement — idempotency key (`ON CONFLICT DO NOTHING`) |
| `timestamp_utc` | `TIMESTAMPTZ` | UTC time of movement |
| `timestamp_dubai` | `TIMESTAMPTZ` | Movement time in Asia/Dubai (derived on write) |
| `handling_unit` | `TEXT` | 18–20 digit HU barcode |
| `indus_part` | `TEXT` | SAP material number |
| `customer_part` | `TEXT` | Customer part reference |
| `pallet_qty` | `TEXT` | Quantity on pallet |
| `job_no` | `TEXT` | Job / work order number |
| `movement_type` | `TEXT` | `STOCK_IN`, `STOCK_OUT`, or `ERROR` |
| `operator_id` | `TEXT` | PIN-resolved operator identity |
| `device_id` | `TEXT` | Device identifier |
| `sync_status` | `TEXT` | `SYNCED` or `PENDING` |
| `validation_status` | `TEXT` | `VALID`, `OVERRIDE`, `VALIDATION_FAIL`, `DUPLICATE`, etc. |
| `location` | `TEXT` | Current / destination location |
| `from_location` | `TEXT` | Origin location (transfer moves) |
| `to_location` | `TEXT` | Destination location (transfer moves) |

**Indexes:** `event_id` (unique), `handling_unit`, `timestamp_utc`, `movement_type`, `location`

---

## Loading audit

### `loading_headers`

One row per confirmed loading batch.

- `loading_id`: UUID primary key.
- `idempotency_key`: unique client-generated key. Retrying the same confirmed request returns the original load instead of posting duplicate STOCK_OUT movements.
- `status`: `POSTING`, `COMPLETED`, or `FAILED`.
- `source_location`: warehouse location from which HUs are loaded.
- `destination`: always `LOADING` for this workflow.
- `operator_id`, `device_id`: worker audit metadata.
- `supervisor_override`, `override_reason`: recorded only where missing-HU fallback was approved.
- `item_count`, `created_at`, `completed_at`: batch audit fields.

### `loading_items`

One row per HU in a loading batch.

- `resolution = EXISTING_STOCK_IN`: HU already existed and was currently STOCK_IN.
- `resolution = MISSING_HU_FALLBACK`: HU did not exist in warehouse records; a supervisor-approved traceable STOCK_IN then STOCK_OUT pair was created.
- `stock_in_event_id`: populated only for missing-HU fallback.
- `stock_out_event_id`: populated for every loaded HU.

### Movement linkage

`movements.loading_id` and `movements.loading_item_id` are nullable audit linkage fields. Existing normal scanner movements do not use them.

### Policy

- Existing HUs not currently in `STOCK_IN` are blocked.
- A supervisor cannot force-post an existing HU with non-`STOCK_IN` status.
- Only genuinely missing warehouse records may use approved fallback `STOCK_IN → STOCK_OUT`.
- The final loading transaction is atomic: validation failure on any HU rolls back the complete batch.

---

### `pallet_status`
Current state of every Handling Unit ever seen. Upserted on every movement.

| Column | Type | Notes |
|--------|------|-------|
| `handling_unit` | `TEXT PRIMARY KEY` | HU barcode — primary key |
| `current_status` | `TEXT` | `IN_WAREHOUSE` or `OUT_OF_WAREHOUSE` |
| `last_event_id` | `TEXT` | References `movements.event_id` |
| `last_movement_type` | `TEXT` | `STOCK_IN` or `STOCK_OUT` |
| `last_movement_time` | `TIMESTAMPTZ` | Time of most recent movement |
| `indus_part` | `TEXT` | Material number from last movement |
| `pallet_qty` | `TEXT` | Pallet quantity from last movement |
| `last_location` | `TEXT` | Location of most recent movement — used for STOCK_OUT location lock |

---

### `ivt_scans`
IVT assembly line QR code scan log. Completely isolated from the warehouse movement flow.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `BIGSERIAL` | Primary key |
| `code` | `TEXT NOT NULL` | The 33-digit QR code scanned |
| `scanned_date` | `TIMESTAMPTZ` | Scan timestamp in Asia/Dubai |
| `location` | `TEXT` | `M4` or `M5` |

**Indexes:** `code` (for duplicate detection), `scanned_date`

---

### `error_log`
Failed movement attempts, validation errors, and system faults.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `BIGSERIAL` | Primary key |
| `error_id` | `TEXT UNIQUE` | UUID |
| `timestamp_utc` | `TIMESTAMPTZ` | UTC time of error |
| `handling_unit` | `TEXT` | HU involved (nullable) |
| `stage` | `TEXT` | `LOOKUP`, `MOVEMENT`, `SYNC`, `OVERRIDE`, `SYSTEM` |
| `error_code` | `TEXT` | Machine-readable error code |
| `error_message` | `TEXT` | Human-readable detail (max 500 chars) |
| `resolved_flag` | `TEXT` | `NO` or `YES` |

---

### `sap_stock_cache`
Cached SAP stock snapshot used by the Admin SAP Reconciliation tab. Refreshed on demand.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `BIGSERIAL` | Primary key |
| `indus_part` | `TEXT` | SAP material number |
| `sap_qty` | `NUMERIC` | Quantity reported by SAP |
| `customer_part` | `TEXT` | Customer part reference (from OOR) |
| `cached_at` | `TIMESTAMPTZ` | When this row was fetched from SAP |

**Indexes:** `indus_part`, `cached_at`

---

## Views

### `v_stock_summary`
All pallet statuses ordered by last movement time descending. Used by the Pallet Status tab.

```sql
SELECT *
FROM pallet_status
ORDER BY last_movement_time DESC;
```

---

### `v_stock_by_location`
Net stock per material per location (STOCK_IN count minus STOCK_OUT count × pallet_qty). Used by the Stock by Location tab.

```sql
SELECT
  location,
  indus_part,
  SUM(CASE WHEN movement_type = 'STOCK_IN'  THEN pallet_qty::numeric ELSE 0 END)
  - SUM(CASE WHEN movement_type = 'STOCK_OUT' THEN pallet_qty::numeric ELSE 0 END) AS net_qty,
  COUNT(*) FILTER (WHERE movement_type = 'STOCK_IN')  AS in_count,
  COUNT(*) FILTER (WHERE movement_type = 'STOCK_OUT') AS out_count
FROM movements
WHERE movement_type IN ('STOCK_IN','STOCK_OUT')
GROUP BY location, indus_part;
```

---

### `v_movement_velocity`
Daily movement counts by type, cast to Dubai timezone. Used by the Analytics daily chart.

```sql
SELECT
  (timestamp_utc AT TIME ZONE 'Asia/Dubai')::date AS move_date,
  movement_type,
  COUNT(*) AS cnt
FROM movements
GROUP BY move_date, movement_type
ORDER BY move_date;
```

---

### `v_operator_activity`
Total movements and last-active time per operator. Used by the Analytics operator table.

```sql
SELECT
  operator_id,
  COUNT(*) AS total_movements,
  MAX(timestamp_utc) AS last_active
FROM movements
GROUP BY operator_id
ORDER BY total_movements DESC;
```

---

## Validation Rules

### Duplicate Detection
- Repeated `STOCK_IN` is blocked when the same `handling_unit`, `indus_part`, and `pallet_qty` are already the last known pallet state.
- Same `handling_unit` with a different `indus_part` or `pallet_qty` is treated as a new pallet run and allowed.
- `STOCK_OUT` is blocked when the last state is already `OUT_OF_WAREHOUSE`.
- `STOCK_OUT` is blocked when the HU has never been stocked in, unless a supervisor override is used.
- `Location` is excluded from the duplicate fingerprint — the state transition check already prevents double STOCK_INs.
- All movements use `ON CONFLICT (event_id) DO NOTHING` for idempotent offline-sync replay.

### Location Rules
- Every `STOCK_IN` and `STOCK_OUT` must specify a valid location: `M4`, `M5`, `M6`, `M4_STORE`, or `M16_STORE`.
- On `STOCK_OUT`, the selected location **must match** `last_location` in `pallet_status` (prevents out-of-location movements).
- After any `STOCK_OUT`, the pallet is free and may be `STOCK_IN` at any location on the next movement.
- Supervisor override bypasses the location lock entirely.

### IVT Code Validation (5 rules)
1. No non-numeric characters
2. Exactly **33 digits** total length
3. Positions 0–8 = `561469490` (customer code)
4. Positions 9–16 = valid `00DDMMYY` date (day 01–31, month 01–12)
5. Positions 23–32 = `8750722699` or `8750722840` (customer part code)

---

## Schema Setup Scripts

| Script | Purpose | Run order |
|--------|---------|----------|
| `scripts/create_schema.py` | Creates all tables, indexes, and views | 1st |
| `scripts/fix_schema_columns.py` | Adds `from_location`, `to_location`; loosens CHECK constraints | 2nd |
| `scripts/add_event_id_constraint.py` | Adds `UNIQUE` constraint on `event_id` | 3rd |
| `scripts/fix_duplicate_event_ids.py` | Deduplicates `event_id` values before constraint is added | Before 3rd if needed |
| `scripts/migrate_excel_to_pg.py` | One-time migration of historical Excel data to Postgres | Optional |

All scripts are idempotent — safe to re-run.

---

*Indus International FZC — Internal Warehouse Management System*
