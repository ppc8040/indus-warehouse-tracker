# Indus Warehouse Tracker

The production warehouse management web application for **Indus International FZC**. A Flask-based Progressive Web App (PWA) that tracks all stock movements and IVT line scans — backed by **Microsoft SharePoint Excel** (via Graph API) and deployed on **Heroku**.

---

## Modes

The app has two completely independent operating modes accessible from the same interface. A toggle button in the header switches between them with zero overlap in data or logic.

### 1. Warehouse Mode (default)
Handles all pallet-level stock movement at the factory floor.

- **Barcode scanning** — camera-based Code 128 barcode scanner using `html5-qrcode`
- **HU Lookup** — scans or manually enters an 18–20 digit Handling Unit number; checks `Pallet_Status` sheet first, falls back to SAP if the HU hasn't been recorded before
- **SAP Integration** — retrieves Indus Part (Material) and Pallet Quantity from SAP automatically
- **Stock IN / Stock OUT** — posts movements to the `Movement_Log` worksheet via Microsoft Graph API
- **Location selection** — operator picks from M4, M5, M6, M4_STORE, M16_STORE before posting
- **Supervisor Override** — two separate PIN-gated overrides:
  - *SAP-not-found override* — allows manual entry of part + quantity when the HU doesn't exist in SAP
  - *Movement-rejection override* — bypasses a validation rejection with supervisor authorisation
- **Duplicate guard** — all movements are checked against `Movement_Log` before posting; duplicates are blocked with no override option
- **Offline queue** — movements queued in-browser when offline, synced automatically on reconnection via `/api/sync` (capped at 50 events per batch)
- **Device name display** — device ID resolved to a friendly name from the `Device_Names` worksheet

### 2. IVT Line Mode
Dedicated QR code scanning mode for the IVT assembly line. Completely isolated from the warehouse movement flow — shares the Excel workbook but writes to a separate worksheet (`IVT_Line`).

- **QR code scanning** — same `html5-qrcode` camera, separate scanner instance, reads QR / Data Matrix / Code 128 / Code 39
- **5-rule code validation** (client-side instant check + server-side safety net):
  1. No non-numeric characters
  2. Exactly **33 digits** total length
  3. Positions 0–8 must equal `561469490` (customer code)
  4. Positions 9–16 must be a valid `00DDMMYY` date segment (day 01–31, month 01–12)
  5. Positions 23–32 must be `8750722699` or `8750722840` (customer part code)
- **Duplicate check** — Column A of `IVT_Line` is searched for an exact match before allowing entry
- **Unique code** → green card shown; operator selects location (M4 or M5) and taps **ENTER** to record
- **Duplicate code** → orange card shown with the original scan date from Column B; no entry possible
- **Invalid code** → red card shown with the specific validation error; operator taps **SCAN AGAIN**
- **Three-column write** — each accepted entry writes `[Code, Scanned Date, Location]` to `IVT_Line`
- **Date format** — Dubai local time, `DD/MM/YYYY HH:MM:SS AM/PM` (same format as Warehouse Mode)

---

## Excel Workbook Structure

All data lives in **`Indus Warehouse Stock Movement.xlsx`** on SharePoint (`Indus_USA` site).

| Worksheet | Used by | Purpose |
|-----------|---------|---------|
| `Movement_Log` | Warehouse Mode | All STOCK_IN / STOCK_OUT records |
| `Pallet_Status` | Warehouse Mode | Current status per HU (checked before SAP) |
| `Error_Log` | Warehouse Mode | Failed movement attempts |
| `Device_Names` | Warehouse Mode | Device ID → friendly name mapping |
| `IVT_Line` | IVT Line Mode | QR scan log (Code, Scanned Date, Location) |

---

## Architecture

```
Worker (Browser / PWA)
    ↓ PIN auth / no SSO
Flask App (Heroku)
    ↓ Microsoft Graph API
SharePoint Excel (Indus_USA site)
└── Indus Warehouse Stock Movement.xlsx
    ├── Movement_Log
    ├── Pallet_Status
    ├── Error_Log
    ├── Device_Names
    └── IVT_Line
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
| `POST` | `/api/lookup-hu` | Look up HU from Pallet_Status or SAP |
| `POST` | `/api/sync` | Sync offline-queued events (max 50/batch) |
| `POST` | `/api/validate-pin` | Validate supervisor PIN |

### IVT Line Mode
| Method | Route | Purpose |
|--------|-------|---------|
| `POST` | `/api/ivt-check` | Validate + duplicate-check a QR code |
| `POST` | `/api/ivt-enter` | Write a confirmed unique code to `IVT_Line` |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, Flask |
| Auth | Static PIN (workers), username/password (admin) |
| Excel backend | Microsoft Graph API — workbook sessions, `append_row`, `find_row_as_dict` |
| Scanner | `html5-qrcode` v2.3.8 |
| Deployment | Heroku (Procfile + `requirements.txt`) |
| PWA | Service Worker + Web App Manifest |
| Timezone | `zoneinfo` — all timestamps in Asia/Dubai |

---

## Project Structure

```
indus-warehouse-tracker/
├── app/
│   ├── config.py              # All config — credentials, sheet names, PINs
│   ├── routes/
│   │   ├── api.py             # All JSON API routes (warehouse + IVT)
│   │   ├── admin.py           # Admin dashboard routes
│   │   └── worker.py          # Worker-facing page routes
│   ├── services/
│   │   ├── graph_client.py    # MS Graph — Excel read/write helpers
│   │   ├── movement_service.py# Warehouse movement business logic
│   │   └── sap_client.py      # SAP HU lookup
│   └── utils/
│       └── validators.py      # PIN and HU validation helpers
├── static/
│   ├── css/worker.css
│   ├── js/
│   │   ├── app.js             # Warehouse mode JS
│   │   └── offline.js         # Offline queue logic
│   └── icons/
├── templates/
│   ├── worker/index.html      # Worker PWA (Warehouse + IVT Line)
│   └── admin/                 # Admin templates
├── Procfile
├── requirements.txt
└── run.py
```

---

## Local Setup

```bash
pip install -r requirements.txt
python run.py
```

Configuration is hardcoded in `app/config.py` (same pattern as `Indus-Open-Order-Report-CUST.py`). No environment variable file needed for local runs.

---

## Relationship to Other Repos

| Repo | Role |
|------|------|
| `indus-warehouse-tracker` | ✅ **This repo** — production warehouse + IVT line app |
| `new_try` | Portal Release Tracker — FG release management with PO allocation |
| `fg-variance-email` | Automated variance email reporter (reads same SharePoint workbook) |
| `indus-inventory-deprecated` | ❌ Deprecated predecessor (Heroku + PostgreSQL) |
| `try1` | ❌ Archived prototype |

---

## Recent Changes

| Date | Change |
|------|--------|
| April 2026 | **IVT Line mode** — new fully isolated QR scanning mode with 5-rule code validation, duplicate detection, M4/M5 location tagging, and writes to the `IVT_Line` worksheet |
| April 2026 | **IVT Location Modal** — separate M4/M5 location picker for IVT mode (independent from the warehouse location modal) |
| April 2026 | **Server-side IVT validation** — `_ivt_validate_code()` in `api.py` mirrors client-side checks as a safety net |
| April 2026 | **Pallet_Status priority lookup** — `/api/lookup-hu` checks `Pallet_Status` before calling SAP to handle label vs SAP quantity discrepancies |
| April 2026 | Dubai timezone formatting (`_fmt_dubai`) — all timestamps stored as `DD/MM/YYYY HH:MM:SS AM/PM` |

---

*Indus International FZC — Internal Warehouse Management System*
