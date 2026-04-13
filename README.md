# Indus FG Warehouse Tracker

Pallet-wise finished goods warehouse tracking system for Indus Air factory workers.  
Mobile-first PWA with phone-camera barcode scanning. Backend: Python Flask on Heroku.  
Data storage: Microsoft Excel Online on SharePoint via Graph API.

---

## Architecture

```
Phone Browser (PWA)          Heroku (Flask + Gunicorn)         SharePoint Excel Online
┌──────────────────┐        ┌─────────────────────────┐       ┌──────────────────────┐
│  Html5-QRcode    │──API──▶│  /api/lookup-hu          │──────▶│  Pallet_Status sheet  │
│  Barcode Scanner │        │  /api/movement           │──────▶│  Movement_Log sheet   │
│  IndexedDB Queue │        │  /api/sync               │──────▶│  Error_Log sheet      │
│  Service Worker  │        │  /api/validate-pin       │       └──────────────────────┘
└──────────────────┘        │  /admin (dashboard)      │
                            │  /health                 │
                            │  Graph Client (MSAL)     │──────▶ Microsoft Entra (Auth)
                            └─────────────────────────┘
```

---

## Worker Flow

```
Scan barcode (camera) or type HU manually → tap LOOKUP
       │
       ▼
  SAP lookup (/api/lookup-hu)
       │
       ├── HU found in Pallet_Status? ─────► Return stored Material + Qty
       │
       ├── HU found in SAP? ───────────────► Return SAP Material + Qty
       │
       └── HU not found anywhere? ─────────► SAP-Not-Found supervisor override modal
                                               Supervisor enters PIN → verified by /api/validate-pin
                                               Worker enters Indus Part + Qty manually
       │
       ▼
  Select Location (M4 / M5 / M6 / M4_STORE / M16_STORE)
       │
       ▼
  Tap STOCK IN or STOCK OUT
       │
       ▼
  State Check ──── Invalid transition? ──► Show error + offer supervisor override
       │
       ▼
  Write Movement_Log row
  Update Pallet_Status row
       │
       ▼
  Show SUCCESS → auto-return to scan screen
```

### Offline Mode
- If no internet: events saved to phone's IndexedDB
- Yellow badge shows pending count
- Auto-syncs when internet returns
- Idempotency keys prevent duplicate writes

### Supervisor Override — Two scenarios

| Scenario | Trigger | Flow |
|---|---|---|
| **SAP-Not-Found** | HU_NOT_FOUND returned by lookup | Modal: supervisor enters PIN → `/validate-pin` issues one-use token → worker fills Indus Part + Qty → movement posted with token |
| **Transition Rejection** | e.g. STOCK_IN when HU already IN | Modal: supervisor enters numeric PIN directly → sent with movement POST |

All overrides are audit-logged with `Validation_Status = OVERRIDE`.

---

## Device Identity

Each device generates a stable random `deviceId` (e.g. `DEV-XXXXXABC`) stored in `localStorage` on first use. This ID is sent as both `operator_id` and `device_id` in every movement payload. No name registration or login is required — workers simply pick up a device and scan.

> **Note:** If `localStorage` is cleared (e.g. browser data wipe), a new `deviceId` is generated automatically. The device remains fully functional; only the ID changes in the log.

---

## Quick Start (Local Development)

```bash
# Clone and install
cd indus-warehouse-tracker
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your real credentials

# Run
python wsgi.py
# Open http://localhost:5000
```

---

## Setup Step 1: Microsoft Entra App Registration

1. Go to **Azure Portal → Microsoft Entra ID → App registrations → New registration**
2. Name: `Indus Warehouse Tracker`
3. Supported account types: **Single tenant** (this organization only)
4. Click **Register**
5. Copy the **Application (client) ID** → this is `GRAPH_CLIENT_ID`
6. Copy the **Directory (tenant) ID** → this is `GRAPH_TENANT_ID`
   - For Indus Air: `ed97e9bb-e119-4bd4-ab00-307d64bdf908`

### Create Client Secret
7. Go to **Certificates & secrets → New client secret**
8. Description: `warehouse-tracker-prod`
9. Expiry: 24 months
10. Copy the secret **Value** (not ID) → this is `GRAPH_CLIENT_SECRET`

### Set API Permissions
11. Go to **API permissions → Add a permission → Microsoft Graph → Application permissions**
12. Add these permissions:
    - `Sites.ReadWrite.All`
    - `Files.ReadWrite.All`
13. Click **Grant admin consent** for the organization
14. Verify all permissions show green checkmarks

> **NOTE:** You can reuse your existing App Registration (client ID `a2239be7-12cd-442d-983f-ea7e316ef767`) if it already has these permissions.

---

## Setup Step 2: SharePoint Excel Workbook

The workbook `Indus Warehouse Stock Movement.xlsx` is already in:  
`https://indusair.sharepoint.com/sites/Indus_USA/Shared Documents/`

### Find the Site ID
The Indus_USA site ID is already known:
```
201c767f-23cc-4eff-b76c-47a275706531,094b21f9-6ed4-47c3-a751-3b217a18b98e
```

To verify or find a different site ID:
```
GET https://graph.microsoft.com/v1.0/sites/indusair.sharepoint.com:/sites/Indus_USA
```
The response `id` field contains the site ID.

### Required Worksheet Structure

The workbook must have exactly these 3 worksheets with these exact headers in Row 1:

#### Movement_Log (12 columns A–L)
| A | B | C | D | E | F | G | H | I | J | K | L |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Event_ID | Timestamp_UTC | Handling_Unit | Indus_Part | Customer_Part | Pallet_Qty | Job_No | Movement_Type | Operator_ID | Device_ID | Sync_Status | Validation_Status |

- Append-only. Workers cannot edit historical rows.
- `Operator_ID` and `Device_ID` are both set to the device's auto-generated `deviceId`.
- The app writes here automatically on every movement.

#### Pallet_Status (7 columns A–G)
| A | B | C | D | E | F | G |
|---|---|---|---|---|---|---|
| Handling_Unit | Current_Status | Last_Event_ID | Last_Movement_Type | Last_Movement_Time | Indus_Part | Pallet_Qty |

- One row per handling unit. Auto-created/updated on movement.
- `Current_Status`: `IN_WAREHOUSE` or `OUT_OF_WAREHOUSE`
- `Indus_Part` and `Pallet_Qty` are stored here and returned on repeat lookups — this is how label vs SAP discrepancies are handled (recorded value takes priority over SAP).

#### Error_Log (7 columns A–G)
| A | B | C | D | E | F | G |
|---|---|---|---|---|---|---|
| Error_ID | Timestamp_UTC | Handling_Unit | Stage | Error_Code | Error_Message | Resolved_Flag |

- Auto-populated on any failed validation, invalid transition, duplicate attempt, or sync failure.

---

## Setup Step 3: Deploy to Heroku

```bash
# Login
heroku login

# Create app
heroku create indus-warehouse-tracker

# Set config vars
heroku config:set SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
heroku config:set GRAPH_TENANT_ID=ed97e9bb-e119-4bd4-ab00-307d64bdf908
heroku config:set GRAPH_CLIENT_ID=your-client-id
heroku config:set GRAPH_CLIENT_SECRET=your-client-secret
heroku config:set SHAREPOINT_SITE_ID="201c767f-23cc-4eff-b76c-47a275706531,094b21f9-6ed4-47c3-a751-3b217a18b98e"
heroku config:set WORKBOOK_FILENAME="Indus Warehouse Stock Movement.xlsx"
heroku config:set SUPERVISOR_PIN=your-pin
heroku config:set ADMIN_USERNAME=admin
heroku config:set ADMIN_PASSWORD=your-strong-password

# Deploy
git init
git add .
git commit -m "Initial deploy"
heroku git:remote -a indus-warehouse-tracker
git push heroku main

# Verify
heroku open
heroku logs --tail
```

### Verify the deployment:
- `https://your-app.herokuapp.com/` — Worker scan page
- `https://your-app.herokuapp.com/health` — Health check + Graph API status
- `https://your-app.herokuapp.com/admin` — Admin dashboard (requires login)

---

## Validation Rules

| Rule | Behavior |
|------|----------|
| Missing Indus Part | Rejected. Error logged. |
| Stock IN when same HU + same part + same qty already IN | Blocked (supervisor override available) |
| Stock IN when same HU but different part or qty | Allowed as a new pallet run |
| Stock OUT when already OUT | Blocked (supervisor override available) |
| Stock OUT when never stocked IN | Blocked (supervisor override available) |
| Duplicate event (same HU + type + idempotency key) | Blocked silently — no override offered |
| HU not found in SAP or Pallet_Status | SAP-Not-Found override modal (supervisor PIN required) |
| Network failure during write | Queued to IndexedDB. Auto-retry on reconnect. |

---

## Project Structure

```
indus-warehouse-tracker/
├── app/
│   ├── __init__.py              # Flask app factory
│   ├── config.py                # All config from env vars
│   ├── routes/
│   │   ├── worker.py            # PWA page serving
│   │   ├── api.py               # JSON API endpoints
│   │   ├── admin.py             # Admin dashboard (auth protected)
│   │   └── health.py            # Health check
│   ├── services/
│   │   ├── graph_client.py      # Graph API + workbook sessions
│   │   ├── sap_client.py        # SAP HU lookup
│   │   └── movement_service.py  # Business logic
│   └── utils/
│       ├── validators.py        # Input validation
│       └── idempotency.py       # Duplicate prevention
├── static/
│   ├── css/
│   │   ├── worker.css           # Mobile-first worker styles
│   │   └── admin.css            # Admin dashboard styles
│   ├── js/
│   │   ├── app.js               # Main worker app logic
│   │   └── offline.js           # IndexedDB offline queue
│   └── icons/
│       ├── icon-192.png         # PWA icon
│       └── icon-512.png         # PWA icon
├── templates/
│   ├── worker/
│   │   ├── index.html           # Main worker page
│   │   ├── manifest.json        # PWA manifest
│   │   └── sw.js                # Service worker
│   └── admin/
│       └── dashboard.html       # Admin dashboard
├── wsgi.py                      # Gunicorn entry point
├── Procfile                     # Heroku process definition
├── runtime.txt                  # Python version
├── requirements.txt             # Python dependencies
├── .env.example                 # Environment variable template
└── README.md                    # This file
```

---

## Test Checklist

### Pre-Deployment
- [ ] All Heroku config vars set
- [ ] Graph API app registration has `Sites.ReadWrite.All` + `Files.ReadWrite.All`
- [ ] Admin consent granted in Azure
- [ ] Excel workbook has all 3 sheets with correct headers in Row 1

### Functional Tests
- [ ] `/health` returns 200 with `graph_api: connected`
- [ ] Scan barcode → SAP data auto-populates (HU, Indus Part, Qty)
- [ ] Scan barcode for known HU → Pallet_Status values returned (not SAP)
- [ ] Manual HU entry → LOOKUP button works identically to camera scan
- [ ] Stock IN on fresh pallet → success, Pallet_Status shows IN_WAREHOUSE
- [ ] Stock OUT after Stock IN → success, Pallet_Status shows OUT_OF_WAREHOUSE
- [ ] Stock IN again when same HU + part + qty already IN → blocked with error
- [ ] Stock IN with same HU but different part or qty → allowed
- [ ] Stock OUT when already OUT → blocked with error
- [ ] Stock OUT when never IN → blocked with error
- [ ] HU not in SAP → SAP-Not-Found modal appears
- [ ] SAP-Not-Found: correct supervisor PIN → fields step shown → movement posts
- [ ] SAP-Not-Found: wrong supervisor PIN → error message, can retry immediately
- [ ] Transition rejection: supervisor override with correct PIN → movement succeeds
- [ ] Transition rejection: wrong PIN → error, can retry immediately
- [ ] Double-tap same button quickly → only one event recorded (idempotency)
- [ ] Turn off WiFi → event saved offline → turn WiFi on → auto-syncs
- [ ] Pending badge shows count while offline, clears after sync
- [ ] Admin dashboard loads with movement history
- [ ] Admin dashboard shows pallet statuses and error log

### Excel Verification
- [ ] Movement_Log gets new rows on each successful movement
- [ ] Pallet_Status row updates (not duplicates) per HU
- [ ] Error_Log gets rows on validation failures
- [ ] Event_ID is unique per movement
- [ ] Timestamps are stored in Dubai display time format
- [ ] `Operator_ID` and `Device_ID` columns show device's auto-generated ID (e.g. `DEV-XXXXXABC`)

---

## Security Notes

- All Graph API auth is server-side (MSAL client credentials). No secrets in frontend.
- Admin dashboard uses HTTP Basic Auth — set a strong `ADMIN_PASSWORD`.
- Supervisor PIN is a short numeric code for factory floor usability. Stored as a Heroku config var (`SUPERVISOR_PIN`). No lockout is applied — wrong PINs simply show an error and allow retry.
- SAP-Not-Found overrides use a server-issued one-use token (32-char hex, 10-minute TTL) so the PIN is never sent with the movement payload directly.
- HTTPS enforced by Heroku by default.
- No user accounts or login — this is a factory tool, not a multi-tenant app. Device identity is auto-generated (`deviceId`) and requires no registration.
