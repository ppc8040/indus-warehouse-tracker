# Indus FG Warehouse Tracker

Pallet-wise finished goods warehouse tracking system for Indus Air factory workers.  
Mobile-first PWA with phone-camera barcode scanning. Backend: Python Flask on Heroku.  
Data storage: Microsoft Excel Online on SharePoint via Graph API.

---

## Architecture

```
Phone Browser (PWA)          Heroku (Flask + Gunicorn)         SharePoint Excel Online
┌──────────────────┐        ┌─────────────────────────┐       ┌──────────────────────┐
│  ZXing Barcode   │──API──▶│  /api/lookup             │──────▶│  Master_Parts sheet   │
│  Scanner         │        │  /api/movement           │──────▶│  Movement_Log sheet   │
│  IndexedDB Queue │        │  /api/sync               │──────▶│  Pallet_Status sheet  │
│  Service Worker  │        │  /admin (dashboard)      │──────▶│  Error_Log sheet      │
└──────────────────┘        │  /health                 │       └──────────────────────┘
                            │                          │
                            │  Graph Client (MSAL)     │──────▶ Microsoft Entra (Auth)
                            └─────────────────────────┘
```

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

The workbook must have exactly these 4 worksheets with these exact headers in Row 1:

#### Master_Parts (7 columns A-G)
| A | B | C | D | E | F | G |
|---|---|---|---|---|---|---|
| Handling_Unit | Indus_Part | Customer_Part | Pallet_Qty | Job_No | Description | Active_Flag |

- `Handling_Unit`: The barcode value on pallet labels (primary key)
- `Active_Flag`: Must be `YES` for pallets to be scanned. Set `NO` to deactivate.
- Populate this sheet with your pallet master data before using the app.

#### Movement_Log (12 columns A-L)
| A | B | C | D | E | F | G | H | I | J | K | L |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Event_ID | Timestamp_UTC | Handling_Unit | Indus_Part | Customer_Part | Pallet_Qty | Job_No | Movement_Type | Operator_ID | Device_ID | Sync_Status | Validation_Status |

- Append-only. Workers cannot edit historical rows.
- The app writes here automatically on every movement.

#### Pallet_Status (5 columns A-E)
| A | B | C | D | E |
|---|---|---|---|---|
| Handling_Unit | Current_Status | Last_Event_ID | Last_Movement_Type | Last_Movement_Time |

- One row per pallet. Auto-created/updated on movement.
- `Current_Status`: `IN_WAREHOUSE` or `OUT_OF_WAREHOUSE`

#### Error_Log (7 columns A-G)
| A | B | C | D | E | F | G |
|---|---|---|---|---|---|---|
| Error_ID | Timestamp_UTC | Handling_Unit | Stage | Error_Code | Error_Message | Resolved_Flag |

- Auto-populated on any failed lookup, invalid transition, duplicate attempt, or sync failure.

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

## Worker Flow

```
Scan Pallet Barcode
       │
       ▼
  Lookup Master_Parts ──── Unknown? ──► "Unknown pallet" error
       │
       ▼
  Show pallet details
  (Indus Part, Customer Part, Qty, Job No, Description)
       │
       ▼
  Tap STOCK IN or STOCK OUT
       │
       ▼
  State Check ──── Invalid transition? ──► Show error + offer override
       │
       ▼
  Write Movement_Log row
  Update Pallet_Status row
       │
       ▼
  Show SUCCESS (green) with timestamp
       │
       ▼
  Ready for next scan
```

### Offline Mode
- If no internet: events saved to phone's IndexedDB
- Yellow badge shows pending count
- Auto-syncs when internet returns (30-second polling)
- Idempotency keys prevent duplicate writes

### Supervisor Override
- Tap 🔑 SUPERVISOR OVERRIDE button
- Enter PIN + select movement type + type reason
- Overrides bypass state transition blocks (e.g., double Stock IN)
- All overrides are audit-logged with `Validation_Status = OVERRIDE`

---

## Validation Rules

| Rule | Behavior |
|------|----------|
| Unknown HU | Rejected. Error logged. |
| Inactive pallet (Active_Flag ≠ YES) | Rejected. Error logged. |
| Stock IN when already IN | Blocked (override available) |
| Stock OUT when already OUT | Blocked (override available) |
| Stock OUT when never stocked IN | Blocked (override available) |
| Duplicate event (same HU + type + timestamp) | Blocked by idempotency. |
| Network failure during write | Queued offline. Auto-retry. |

---

## Project Structure

```
indus-warehouse-tracker/
├── app/
│   ├── __init__.py          # Flask app factory
│   ├── config.py            # All config from env vars
│   ├── routes/
│   │   ├── worker.py        # PWA page serving
│   │   ├── api.py           # JSON API endpoints
│   │   ├── admin.py         # Admin dashboard (auth protected)
│   │   └── health.py        # Health check
│   ├── services/
│   │   ├── graph_client.py  # Graph API + workbook sessions
│   │   └── movement_service.py  # Business logic
│   └── utils/
│       ├── validators.py    # Input validation
│       └── idempotency.py   # Duplicate prevention
├── static/
│   ├── css/
│   │   ├── worker.css       # Mobile-first worker styles
│   │   └── admin.css        # Admin dashboard styles
│   ├── js/
│   │   ├── app.js           # Main worker app logic
│   │   └── offline.js       # IndexedDB offline queue
│   └── icons/
│       ├── icon-192.png     # PWA icon
│       └── icon-512.png     # PWA icon
├── templates/
│   ├── worker/
│   │   ├── index.html       # Main worker page
│   │   ├── manifest.json    # PWA manifest
│   │   └── sw.js            # Service worker
│   └── admin/
│       └── dashboard.html   # Admin dashboard
├── wsgi.py                  # Gunicorn entry point
├── Procfile                 # Heroku process definition
├── runtime.txt              # Python version
├── requirements.txt         # Python dependencies
├── .env.example             # Environment variable template
└── README.md                # This file
```

---

## Test Checklist

### Pre-Deployment
- [ ] All Heroku config vars set
- [ ] Graph API app registration has `Sites.ReadWrite.All` + `Files.ReadWrite.All`
- [ ] Admin consent granted in Azure
- [ ] Excel workbook has all 4 sheets with correct headers in Row 1
- [ ] At least 1 row of test data in Master_Parts (Active_Flag = YES)

### Functional Tests
- [ ] `/health` returns 200 with `graph_api: connected`
- [ ] Scan a known HU → details load correctly
- [ ] Scan an unknown HU → "Unknown pallet" error
- [ ] Scan an inactive pallet (Active_Flag = NO) → "INACTIVE" error
- [ ] Stock IN on fresh pallet → success, Pallet_Status shows IN_WAREHOUSE
- [ ] Stock OUT after Stock IN → success, Pallet_Status shows OUT_OF_WAREHOUSE
- [ ] Stock IN again when already IN → blocked with error message
- [ ] Stock OUT when already OUT → blocked with error message
- [ ] Stock OUT when never IN → blocked with error message
- [ ] Supervisor override with correct PIN → movement succeeds
- [ ] Supervisor override with wrong PIN → "Invalid PIN" error
- [ ] Override reason captured in Movement_Log
- [ ] Double-tap same button quickly → only one event recorded (idempotency)
- [ ] Turn off WiFi → event saved offline → turn WiFi on → auto-syncs
- [ ] Pending badge shows count while offline
- [ ] Admin dashboard loads with movement history
- [ ] Admin dashboard shows pallet statuses
- [ ] Admin dashboard shows error log
- [ ] Admin dashboard stats are accurate
- [ ] PWA installs to home screen
- [ ] App works after home-screen install

### Excel Verification
- [ ] Movement_Log gets new rows on each successful movement
- [ ] Pallet_Status row updates (not duplicates) per HU
- [ ] Error_Log gets rows on validation failures
- [ ] Event_ID is unique per movement
- [ ] Timestamps are UTC ISO 8601

---

## Security Notes

- All Graph API auth is server-side (MSAL client credentials). No secrets in frontend.
- Admin dashboard uses HTTP Basic Auth — set a strong `ADMIN_PASSWORD`.
- Supervisor PIN is simple 4-digit for factory floor usability. Store as Heroku config var.
- HTTPS is enforced by Heroku by default.
- No user accounts or login — this is a factory tool, not a multi-tenant app.
