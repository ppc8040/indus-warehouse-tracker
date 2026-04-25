# Indus Warehouse Tracker

The production warehouse management web application for **Indus International FZC**. A Flask-based Progressive Web App (PWA) that tracks all stock movements and IVT line scans — backed by a **PostgreSQL** database (Heroku Postgres) and deployed on **Heroku**.

---

## Modes

The app has two completely independent operating modes accessible from the same interface. A toggle button in the header switches between them with zero overlap in data or logic.

### 1. Warehouse Mode (default)
Handles all pallet-level stock movement at the factory floor.

- **Barcode scanning** — camera-based Code 128 barcode scanner using `html5-qrcode`
- **HU Lookup** — scans or manually enters an 18–20 digit Handling Unit number; checks `pallet_status` table first, falls back to SAP if the HU hasn't been recorded before
- **SAP Integration** — retrieves Indus Part (Material) and Pallet Quantity from SAP automatically
- **Stock IN / Stock OUT** — posts movements to the `movements` table via `db_client.insert_movement()`
- **Location selection** — operator picks from M4, M5, M6, M4_STORE, M16_STORE before posting
- **Supervisor Override** — two separate PIN-gated overrides:
  - *SAP-not-found override* — allows manual entry of part + quantity when the HU doesn't exist in SAP
  - *Movement-rejection override* — bypasses a validation rejection with supervisor authorisation
- **Duplicate guard** — all movements are checked against `movements` before posting; duplicates are blocked with no override option (`ON CONFLICT (event_id) DO NOTHING`)
- **Offline queue** — movements queued in-browser when offline, synced automatically on reconnection via `/api/sync` (capped at 50 events per batch)
- **Device name display** — device ID resolved to a friendly name from config

### 2. IVT Line Mode
Dedicated QR code scanning mode for the IVT assembly line. Completely isolated from the warehouse movement flow — writes to the `ivt_scans` table.

- **QR code scanning** — same `html5-qrcode` camera, separate scanner instance, reads QR / Data Matrix / Code 128 / Code 39
- **5-rule code validation** (client-side instant check + server-side safety net):
  1. No non-numeric characters
  2. Exactly **33 digits** total length
  3. Positions 0–8 must equal `561469490` (customer code)
  4. Positions 9–16 must be a valid `00DDMMYY` date segment (day 01–31, month 01–12)
  5. Positions 23–32 must be `8750722699` or `8750722840` (customer part code)
- **Duplicate check** — `ivt_scans` table is searched for an exact match before allowing entry
- **Unique code** → green card shown; operator selects location (M4 or M5) and taps **ENTER** to record
- **Duplicate code** → orange card shown with the original scan date; no entry possible
- **Invalid code** → red card shown with the specific validation error; operator taps **SCAN AGAIN**
- **Three-column write** — each accepted entry writes `[code, scanned_date, location]` to `ivt_scans`
- **Date format** — Dubai local time, ISO 8601 stored in Postgres as `TIMESTAMPTZ`

---

## Database Schema

All data lives in **Heroku Postgres**. Schema is created by `scripts/create_schema.py`.

| Table | Purpose |
|-------|---------|
| `movements` | All STOCK_IN / STOCK_OUT records + error rows |
| `pallet_status` | Current status per HU (upserted on every movement) |
| `ivt_scans` | IVT QR scan log (code, scanned_date, location) |
| `error_log` | Failed movement attempts with stage and error code |

### Views

| View | Purpose |
|------|---------|
| `v_stock_summary` | All pallet statuses ordered by last movement time |
| `v_stock_by_location` | Pallet count + total qty grouped by location and part |
| `v_movement_velocity` | Daily movement counts by type (Dubai timezone) |
| `v_operator_activity` | Movement totals and last-active time per operator |

### movements columns

| Column | Type | Notes |
|--------|------|-------|
| `id` | BIGSERIAL | Primary key |
| `event_id` | TEXT UNIQUE | UUID per movement, used for idempotency |
| `timestamp_utc` | TIMESTAMPTZ | UTC time of movement |
| `handling_unit` | TEXT | 18–20 digit HU barcode |
| `indus_part` | TEXT | SAP material number |
| `customer_part` | TEXT | Customer part reference |
| `pallet_qty` | TEXT | Quantity on pallet |
| `job_no` | TEXT | Job / work order number |
| `movement_type` | TEXT | `STOCK_IN`, `STOCK_OUT`, or `ERROR` |
| `operator_id` | TEXT | PIN-resolved operator identity |
| `device_id` | TEXT | Device identifier |
| `sync_status` | TEXT | `SYNCED` or `PENDING` |
| `validation_status` | TEXT | `VALID`, `OVERRIDE`, `VALIDATION_FAIL`, `DUPLICATE`, etc. |
| `location` | TEXT | Current / destination location |
| `from_location` | TEXT | Origin location (transfers) |
| `to_location` | TEXT | Destination location (transfers) |

---

## Architecture

```
Worker (Browser / PWA)
    ↓ PIN auth / no SSO
Flask App (Heroku)
    ↓ db_client.py (psycopg2 ThreadedConnectionPool)
Heroku Postgres
    ├── movements
    ├── pallet_status
    ├── ivt_scans
    └── error_log
```

```
Flask App (Heroku)
    ↓ sap_client.py
SAP S/4HANA Public Cloud (HU lookup only)
```

```
Admin (Browser)
    ↓ username + password
Flask App (Heroku)
    └── Admin routes (dashboard, logs, device management)
```

---

## API Endpoints

### Warehouse Mode
| Method | Route | Purpose |
|--------|-------|---------|
| `POST` | `/api/movement` | Post a STOCK_IN or STOCK_OUT movement |
| `POST` | `/api/lookup-hu` | Look up HU from pallet_status or SAP |
| `POST` | `/api/sync` | Sync offline-queued events (max 50/batch) |
| `POST` | `/api/validate-pin` | Validate supervisor PIN |

### IVT Line Mode
| Method | Route | Purpose |
|--------|-------|---------|
| `POST` | `/api/ivt-check` | Validate + duplicate-check a QR code |
| `POST` | `/api/ivt-enter` | Write a confirmed unique code to `ivt_scans` |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, Flask |
| Database | PostgreSQL (Heroku Postgres), psycopg2 |
| Auth | Static PIN (workers), username/password (admin) |
| SAP Integration | SAP S/4HANA Public Cloud API (HU lookup) |
| Scanner | `html5-qrcode` v2.3.8 |
| Deployment | Heroku (Procfile + `requirements.txt`) |
| PWA | Service Worker + Web App Manifest |
| Timezone | `zoneinfo` — all timestamps in Asia/Dubai |

---

## Project Structure

```
indus-warehouse-tracker/
├── app/
│   ├── config.py                  # All config — credentials, PINs
│   ├── routes/
│   │   ├── api.py                 # All JSON API routes (warehouse + IVT)
│   │   ├── admin.py               # Admin dashboard routes
│   │   └── worker.py              # Worker-facing page routes
│   ├── services/
│   │   ├── db_client.py           # PostgreSQL data access layer
│   │   ├── movement_service.py    # Warehouse movement business logic
│   │   └── sap_client.py         # SAP HU lookup
│   └── utils/
│       └── validators.py          # PIN and HU validation helpers
├── scripts/
│   ├── create_schema.py           # One-time schema creation (tables + views)
│   ├── add_event_id_constraint.py # Adds UNIQUE constraint on event_id
│   ├── fix_duplicate_event_ids.py # Deduplicates event_ids before constraint
│   ├── fix_schema_columns.py      # Adds from_location/to_location, fixes CHECKs
│   └── migrate_excel_to_pg.py    # One-time Excel → Postgres data migration
├── static/
│   ├── css/worker.css
│   ├── js/
│   │   ├── app.js                 # Warehouse mode JS
│   │   └── offline.js             # Offline queue logic
│   └── icons/
├── templates/
│   ├── worker/index.html          # Worker PWA (Warehouse + IVT Line)
│   └── admin/                     # Admin templates
├── Procfile
├── requirements.txt
└── run.py
```

---

## Local Setup

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://...
python run.py
```

`DATABASE_URL` must be set to a valid PostgreSQL connection string. On Heroku this is set automatically by the Postgres add-on.

---

## Database Setup (fresh environment)

Run these scripts **in order** once:

```bash
# 1. Create all tables, indexes, and views
heroku run python scripts/create_schema.py --app indus-warehouse-tracker

# 2. Fix schema columns (from_location, to_location, CHECK constraints)
heroku run python scripts/fix_schema_columns.py --app indus-warehouse-tracker

# 3. Add UNIQUE constraint on event_id for idempotency
heroku run python scripts/add_event_id_constraint.py --app indus-warehouse-tracker

# 4. (Optional) Migrate historical data from Excel
heroku run python scripts/migrate_excel_to_pg.py --app indus-warehouse-tracker
```

All scripts are idempotent — safe to re-run.

---

## Relationship to Other Repos

| Repo | Role |
|------|------|
| `indus-warehouse-tracker` | ✅ **This repo** — production warehouse + IVT line app |
| `new_try` | Portal Release Tracker — FG release management with PO allocation |
| `fg-variance-email` | Automated variance email reporter |
| `indus-inventory-deprecated` | ❌ Deprecated predecessor |
| `try1` | ❌ Archived prototype |

---

## Recent Changes

| Date | Change |
|------|--------|
| April 2026 | **Schema fix** — added `from_location` and `to_location` columns; loosened `validation_status` and `movement_type` CHECK constraints to allow error rows (`fix_schema_columns.py`) |
| April 2026 | **PostgreSQL migration** — replaced Microsoft Graph API / SharePoint Excel backend with Heroku Postgres (`db_client.py`); all movements, pallet statuses, IVT scans, and error logs now stored in Postgres |
| April 2026 | **IVT Line mode** — new fully isolated QR scanning mode with 5-rule code validation, duplicate detection, M4/M5 location tagging, writes to `ivt_scans` table |
| April 2026 | **Pallet_Status priority lookup** — `/api/lookup-hu` checks `pallet_status` table before calling SAP |
| April 2026 | Dubai timezone formatting (`_fmt_dubai`) — all timestamps stored as `TIMESTAMPTZ` in UTC, displayed in Asia/Dubai |

---

*Indus International FZC — Internal Warehouse Management System*
