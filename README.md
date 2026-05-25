# Indus Warehouse Tracker

The production warehouse management web application for **Indus International FZC**. A Flask-based Progressive Web App (PWA) that tracks all stock movements and IVT line scans — backed by a **PostgreSQL** database (Heroku Postgres) and deployed on **Heroku**.

---

## Modes

The app has two completely independent operating modes accessible from the same interface. A toggle button in the header switches between them with zero overlap in data or logic.

### 1. Warehouse Mode (default)
Handles all pallet-level stock movement at the factory floor.

- **Barcode scanning** — camera-based Code 128 barcode scanner using `html5-qrcode`
- **HU Lookup** — scans or manually enters an 18–20 digit Handling Unit number; checks `pallet_status` table first, falls back to SAP if the HU hasn't been recorded before
- **SAP Integration** — retrieves Indus Part (Material) and Pallet Quantity from SAP S/4HANA automatically
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

## Admin Dashboard

Password-protected portal at `/admin` for warehouse supervisors. Provides full operational visibility and data export.

### Tabs

| Tab | Contents |
|-----|----------|
| **Movements** | Full movement log, sortable, filterable |
| **Pallet Status** | Current status of every HU seen |
| **Stock by Location** | Net stock (IN − OUT) per material per location; negative stock banner |
| **Total Stock** | All-locations combined stock totals |
| **Analytics** | Charts — daily movement volume, pallets by location, top parts, operator activity |
| **IVT Scans** | IVT scan log with daily scan chart |
| **Errors** | Error log + negative stock alerts |
| **SAP Recon** | Side-by-side SAP vs. scanner stock comparison with variance highlighting |

### Filters
All tabular data can be filtered by: HU Number, Indus Part, Customer (from Open Order Report), Date From/To, Movement Type, and Location.

### Excel-like Table Selection
Every dashboard table supports spreadsheet-style cell interaction:
- **Click** a cell to select it (blue outline highlight)
- **Click and drag** across rows and columns to select a range
- **`Ctrl+C`** copies selected cell(s) to clipboard (tab-separated, newline between rows — pastes cleanly into Excel)
- **Column header click** selects the entire column; row number click selects the entire row
- **Arrow keys** navigate between cells once one is focused
- **`Ctrl+A`** selects all visible cells in the active table
- A **column summary bar** appears during selection showing count, sum, min, max for numeric columns
- An **Export Selection** button appears when cells are selected — exports the selected range to a `.csv` file with proper HU number handling (no scientific notation)
- No editing — all cells are read-only

### Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `1` – `8` | Switch to tab by number |
| `R` | Refresh current tab data |
| `Ctrl+E` | Export current tab to Excel |
| `F` or `/` | Focus the HU Number filter input |
| `Esc` | Clear all filters / close overlay |
| `Ctrl+A` | Select all cells in visible table |
| `Ctrl+C` | Copy selected cell(s) to clipboard |
| `↑ ↓ ← →` | Navigate between cells |
| `Ctrl+F` | Find / highlight text in current table |
| `Ctrl+Shift+R` | Cycle auto-refresh interval (OFF → 30s → 1m → 5m) |
| `?` | Toggle keyboard shortcut help overlay |

### Export
- **Export Excel** — server-side `.xlsx` export via `openpyxl`; HU numbers and long part numbers formatted as text (no approximation)
- **Export Selection** — client-side `.csv` export of the selected cell range; uses `=\"value\"` formula trick for long numeric strings to match server-side behaviour

### SAP Reconciliation Tab
- Fetches live stock data from SAP S/4HANA and compares against scanner totals
- Highlights discrepancies (positive variance, negative variance, SAP-only, scanner-only rows)
- Searchable and filterable; cached with a visible cache-age indicator
- Separate **Refresh from SAP** button to force a live pull

### Auto-Refresh
Dropdown in the header to set automatic data refresh at 30s, 1 min, or 5 min intervals. A circular countdown indicator shows time remaining to next refresh.

### Light / Dark Mode
Full light and dark theme support with a toggle button in the header. Theme is applied via `data-theme` attribute on `<html>`.

---

## Database Schema

All data lives in **Heroku Postgres**. Schema is created by `scripts/create_schema.py`.

See [SCHEMA.md](./SCHEMA.md) for full column-level documentation.

| Table | Purpose |
|-------|---------|
| `movements` | All STOCK_IN / STOCK_OUT records + error rows |
| `pallet_status` | Current status per HU (upserted on every movement) |
| `ivt_scans` | IVT QR scan log (code, scanned_date, location) |
| `error_log` | Failed movement attempts with stage and error code |
| `sap_stock_cache` | Cached SAP stock snapshot for reconciliation |

### Views

| View | Purpose |
|------|---------|
| `v_stock_summary` | All pallet statuses ordered by last movement time |
| `v_stock_by_location` | Net qty (IN − OUT) grouped by location and part |
| `v_movement_velocity` | Daily movement counts by type (Dubai timezone) |
| `v_operator_activity` | Movement totals and last-active time per operator |

---

## Architecture

```
Worker (Browser / PWA)
    ↓ PIN auth
Flask App (Heroku)
    ↓ db_client.py (psycopg2 ThreadedConnectionPool)
Heroku Postgres
    ├── movements
    ├── pallet_status
    ├── ivt_scans
    ├── error_log
    └── sap_stock_cache
```

```
Flask App (Heroku)
    ↓ sap_client.py
SAP S/4HANA Public Cloud
    ├── HU lookup (material + qty)
    └── Stock reconciliation (batch material query)
```

```
Admin (Browser)
    ↓ username + password
Flask App (Heroku)
    └── Admin routes (dashboard, API endpoints, export)
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

### Admin API
| Method | Route | Purpose |
|--------|-------|---------|
| `GET` | `/admin/api/stats` | KPI summary counts |
| `GET` | `/admin/api/movements` | Paginated/filtered movement log |
| `GET` | `/admin/api/pallet-statuses` | Pallet status list |
| `GET` | `/admin/api/stock-by-location` | Stock grouped by location + part |
| `GET` | `/admin/api/total-stock` | All-locations combined stock |
| `GET` | `/admin/api/ivt` | IVT scan log |
| `GET` | `/admin/api/errors` | Error log + negative stock alerts |
| `GET` | `/admin/api/sap-recon` | SAP vs. scanner reconciliation data |
| `GET` | `/admin/api/export` | Excel export (all tabs via `?sheet=` param) |
| `GET` | `/admin/api/connection-test` | DB connectivity check |
| `GET` | `/admin/api/oor-customer-codes` | Open Order Report customer code map |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, Flask |
| Database | PostgreSQL (Heroku Postgres), psycopg2 |
| Auth | Static PIN (workers), username/password (admin) |
| SAP Integration | SAP S/4HANA Public Cloud API (HU lookup + stock reconciliation) |
| Scanner | `html5-qrcode` v2.3.8 |
| Export | `openpyxl` (server-side Excel), CSV blob (client-side selection export) |
| Charts | Chart.js v4.4.3 |
| Deployment | Heroku (Procfile + `requirements.txt`) |
| PWA | Service Worker + Web App Manifest |
| Timezone | `zoneinfo` — all timestamps in Asia/Dubai |

---

## Project Structure

```
indus-warehouse-tracker/
├── app/
│   ├── config.py                  # All config — credentials, PINs, device names
│   ├── routes/
│   │   ├── api.py                 # Worker-facing JSON API (warehouse + IVT)
│   │   ├── admin.py               # Admin dashboard + admin API routes
│   │   └── worker.py              # Worker-facing page routes
│   ├── services/
│   │   ├── db_client.py           # PostgreSQL data access layer (connection pool)
│   │   ├── movement_service.py    # Warehouse movement business logic + validation
│   │   └── sap_client.py         # SAP HU lookup + stock reconciliation
│   └── utils/
│       └── validators.py          # PIN and HU validation helpers
├── scripts/
│   ├── create_schema.py           # One-time schema creation (tables, indexes, views)
│   ├── add_event_id_constraint.py # Adds UNIQUE constraint on event_id
│   ├── fix_duplicate_event_ids.py # Deduplicates event_ids before constraint
│   ├── fix_schema_columns.py      # Adds from_location/to_location, loosens CHECKs
│   └── migrate_excel_to_pg.py    # One-time Excel → Postgres historical data migration
├── static/
│   ├── css/worker.css
│   ├── js/
│   │   ├── app.js                 # Warehouse mode JS
│   │   └── offline.js             # Offline queue logic
│   └── icons/
├── templates/
│   ├── worker/index.html          # Worker PWA (Warehouse + IVT Line modes)
│   └── admin/
│       └── dashboard.html         # Admin dashboard (all tabs, charts, shortcuts)
├── Procfile
├── requirements.txt
├── runtime.txt
├── wsgi.py
├── SCHEMA.md                      # Full PostgreSQL schema reference
└── run.py
```

---

## Local Setup

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://...
python run.py
```

`DATABASE_URL` must point to a valid PostgreSQL instance. On Heroku this is injected automatically by the Postgres add-on.

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
| May 2026 | **Admin QoL** — Excel-like cell selection & range copy on all dashboard tables; full keyboard shortcut system (`1–8` tab switch, `R` refresh, `Ctrl+E` export, `F` filter focus, `Ctrl+A/C`, arrow key nav, `?` help overlay); auto-refresh with countdown; light/dark theme toggle; tab row-count badges; column summary bar; Export Selection with correct HU number handling (`=\"value\"` CSV trick) |
| May 2026 | **SAP Recon tab** — live SAP vs. scanner stock comparison with discrepancy highlighting, cache-age display, search + filter by variance type |
| April 2026 | **Schema fix** — added `from_location` and `to_location` columns; loosened `validation_status` and `movement_type` CHECK constraints (`fix_schema_columns.py`) |
| April 2026 | **PostgreSQL migration** — replaced Microsoft Graph API / SharePoint Excel backend with Heroku Postgres; all data now in `movements`, `pallet_status`, `ivt_scans`, `error_log` |
| April 2026 | **IVT Line mode** — new fully isolated QR scanning mode with 5-rule code validation, duplicate detection, M4/M5 location tagging |
| April 2026 | **Pallet_Status priority lookup** — `/api/lookup-hu` checks `pallet_status` before calling SAP |
| April 2026 | **Dubai timezone** — all timestamps stored as `TIMESTAMPTZ` UTC, displayed in Asia/Dubai |

---

*Indus International FZC — Internal Warehouse Management System*
