# Indus Warehouse Tracker

A Flask-based warehouse scanning and audit application for recording warehouse movements, tracking handling-unit status, posting loading batches, and reviewing operational data through an authenticated admin dashboard.

The application uses PostgreSQL as its primary operational data store. It integrates with SAP for handling-unit and stock data, and with Microsoft SharePoint / Microsoft Graph for customer-code data from the Indus Open Order Report.

## Core capabilities

- Scan and record STOCK_IN and STOCK_OUT movements.
- Track the latest status and location of each handling unit (HU).
- Support Loading mode for moving one or more HUs from a selected warehouse source location to `LOADING`.
- Validate a loading batch before posting it.
- Store loading headers and per-HU loading audit records.
- Record the shipment/container reference entered before a loading is posted.
- Preserve the customer code associated with each loading item.
- Export loading audit data to Excel.
- Display loading records and their per-item audit detail in the admin dashboard.
- Read customer-code mappings from the Indus Open Order Report in SharePoint.
- Provide dashboard views for movements, loadings, pallet status, stock by location, total stock, analytics, IVT scans, BDR scans, errors, and SAP reconciliation.
- Use idempotency controls to help prevent duplicate movement and loading records.

## Architecture

```text
Worker scanner UI
        |
        v
Flask worker + API routes
        |
        +------------------------+
        |                        |
        v                        v
PostgreSQL                  SAP services
        |                        |
        v                        v
Admin dashboard        HU lookup / stock reconciliation
        |
        v
Microsoft Graph / SharePoint
(Open Order Report customer codes)
```

## Technology stack

- Python
- Flask
- PostgreSQL
- psycopg2 connection pooling
- openpyxl for Excel exports
- Microsoft Graph API for SharePoint workbook access
- SAP HTTP services for HU lookup and stock reconciliation
- Chart.js for dashboard analytics charts
- Gunicorn for Heroku deployment

## Repository structure

```text
.
├── app/
│   ├── __init__.py
│   ├── config.py
│   ├── routes/
│   │   ├── admin.py
│   │   ├── api.py
│   │   ├── health.py
│   │   └── worker.py
│   ├── services/
│   │   ├── db_client.py
│   │   ├── graph_client.py
│   │   ├── loading_service.py
│   │   ├── movement_service.py
│   │   ├── sap_client.py
│   │   ├── sap_stock_client.py
│   │   └── sharepoint_master.py
│   └── utils/
├── scripts/
│   ├── add_loading_schema.py
│   ├── add_loading_shipment_reference.py
│   ├── add_hu_metadata_columns.py
│   ├── add_indexes.py
│   ├── create_schema.py
│   ├── migrate_excel_to_pg.py
│   ├── reconcile_pallet_status.py
│   ├── reset_db.py
│   ├── delete_movement.py
│   └── other maintenance and repair scripts
├── static/
│   └── js/
│       ├── app.js
│       └── loading_review.js
├── templates/
│   ├── admin/
│   │   ├── dashboard.html
│   │   └── login.html
│   └── worker/
│       └── index.html
├── .env.example
├── Procfile
├── requirements.txt
├── runtime.txt
├── SCHEMA.md
└── wsgi.py
```

## Main application areas

### Worker scanner

The worker interface is used to scan handling units and process warehouse movements.

Supported workflows include:

- Normal warehouse scanning.
- Stock-in recording.
- Stock-out recording.
- HU lookup and validation.
- Source-location selection for loading operations.
- Loading review before posting.
- Loading reference capture before posting.
- Loading confirmation and posting.

The worker-side loading review shows each scanned HU with its Indus part, quantity, and customer code before the loading batch is posted.

### Loading mode

Loading mode records a batch movement from a selected source location to `LOADING`.

A loading operation stores two audit layers:

1. **Loading header**
   - Loading ID
   - Idempotency key
   - Status
   - Source location
   - Destination
   - Operator
   - Device ID
   - Shipment / container reference
   - Supervisor override flag
   - Override reason
   - Item count
   - Created timestamp
   - Completed timestamp

2. **Loading items**
   - Loading item ID
   - Parent loading ID
   - Handling unit
   - Sequence number
   - Indus part
   - Customer part / customer code
   - Pallet quantity
   - Resolution
   - Fallback STOCK_IN event ID, when applicable
   - STOCK_OUT event ID
   - Creation timestamp

Loading item records are deleted before loading headers when resetting loading test data because `loading_items.loading_id` references `loading_headers.loading_id`.

### Customer-code lookup

Customer codes are sourced from the Indus Open Order Report in SharePoint.

The mapping is:

```text
Indus Part -> Customer Code
```

The admin application reads the Open Order Report through Microsoft Graph and caches the mapping in memory for five minutes to avoid repeated workbook requests.

Customer codes are used for:

- Worker loading review display.
- Dashboard customer filtering.
- Movements table display.
- Pallet-status table display.
- Stock-by-location table display.
- Total-stock table display.
- SAP reconciliation display.
- Loading detail display.
- Loading Excel export.

A failed customer-code lookup is non-blocking: warehouse movement processing and loading posting continue even if the customer-code data is unavailable.

### Admin dashboard

The admin dashboard is available under the `/admin/` route and is protected by session-based admin authentication.

Dashboard tabs include:

| Tab | Purpose |
|---|---|
| Movements | Recent warehouse movement log with filters and export |
| Loadings | Loading audit list, status filter, detail view, and Excel export |
| Pallet Status | Latest derived state for each handling unit |
| Stock by Location | Net stock by Indus part and warehouse location |
| Total Stock | Aggregated warehouse stock by Indus part |
| Analytics | Movement velocity, location breakdown, top parts, and operator activity |
| IVT Scans | IVT scan records |
| Errors | Movement validation errors and negative-stock alerts |
| BDR Scans | BDR scan records and filters |
| SAP Recon | Scanner-vs-SAP stock reconciliation |

The dashboard supports:

- Manual refresh.
- Optional auto-refresh.
- Dark and light themes.
- Tab row-count badges.
- Table sorting.
- Excel-like cell selection.
- Copying selected table cells.
- Selection export to CSV.
- Filtered Excel exports.
- Keyboard shortcuts.

### Loading audit in the dashboard

The Loadings tab lists loading headers and supports filtering by:

- All statuses
- Completed
- Posting
- Failed

Each loading record can be opened to show its item-level audit data, including:

- HU
- Indus part
- Customer code
- Quantity
- Resolution
- Fallback STOCK_IN event
- STOCK_OUT event
- Source
- Destination
- Posted time

The loading detail also displays loading-header information including:

- Load ID
- Source
- Destination
- Shipment / container reference ID
- Operator
- Supervisor override state
- Override reason

Loading audit data can be exported as a formatted Excel workbook.

### SAP reconciliation

The SAP Recon tab compares scanner-derived net stock against SAP stock values.

It shows:

- Indus part
- Customer code
- Customer part
- Scanner net quantity
- SAP Plant 1000 quantity
- SAP Plant 2000 quantity
- Variance between scanner quantity and SAP Plant 1000
- Match, shortage, or excess status

The SAP stock request is cached and polled by the dashboard so that long-running SAP fetches do not block the browser interface.

## Database

PostgreSQL is the primary data store.

Important tables include:

| Table | Purpose |
|---|---|
| `movements` | Immutable warehouse movement events, including STOCK_IN and STOCK_OUT |
| `pallet_status` | Latest derived status and location for each HU |
| `loading_headers` | One audit record per posted loading batch |
| `loading_items` | One audit record per HU within a loading batch |
| `ivt_scans` | IVT barcode scan records |
| `bdr_scans` | BDR barcode scan records |

See [`SCHEMA.md`](SCHEMA.md) for schema documentation and the `scripts/` directory for setup, migration, repair, and maintenance scripts.

## Installation

### Prerequisites

- Python version specified in [`runtime.txt`](runtime.txt)
- PostgreSQL database
- SAP credentials and endpoint configuration
- Microsoft Entra ID application credentials for SharePoint / Microsoft Graph access
- Access to the required SharePoint site and Open Order Report workbook

### Create a local environment

```bash
git clone [https://github.com/ppc8040/indus-warehouse-tracker.git](https://github.com/ppc8040/indus-warehouse-tracker.git)
cd indus-warehouse-tracker

python -m venv .venv
source .venv/bin/activate
```

On Windows:

```bat
.venv\Scripts\activate
```

### Install dependencies

```bash
pip install -r requirements.txt
```

### Configure environment variables

Copy the environment template:

```bash
cp .env.example .env
```

Then set the required values in `.env`.

Do not commit `.env`, production credentials, SAP credentials, database URLs, or Microsoft Graph secrets.

## Environment configuration

Use `.env.example` as the authoritative list of expected configuration variables.

The application requires configuration for these areas:

| Area | Purpose |
|---|---|
| Flask | Session and application configuration |
| Admin | Admin dashboard password |
| PostgreSQL | `DATABASE_URL` for operational data |
| SAP HU lookup | SAP base URL and authentication configuration |
| SAP stock reconciliation | SAP stock endpoint and authentication configuration |
| Microsoft Graph | Tenant ID, client ID, client secret, and SharePoint site configuration |
| SharePoint workbook | Open Order Report filename and lookup configuration |
| Request timeouts | HTTP timeout settings for SAP and Graph calls |

At minimum, ensure the application has a valid `DATABASE_URL` in every environment.

## Database setup

For a new PostgreSQL database, run the relevant schema scripts from the repository root.

Typical initial setup:

```bash
python scripts/create_schema.py
python scripts/add_loading_schema.py
python scripts/add_loading_shipment_reference.py
python scripts/add_hu_metadata_columns.py
python scripts/add_indexes.py
```

Run only the scripts appropriate to your database state. Review each script before using it against an environment containing live operational data.

## Run locally

Set your environment variables, then start Flask:

```bash
flask --app wsgi:app run --debug
```

Or run the WSGI entry point through Gunicorn:

```bash
gunicorn wsgi:app
```

Typical routes:

| Route | Purpose |
|---|---|
| `/` | Worker scanner interface |
| `/api/lookup-hu` | HU lookup endpoint |
| `/api/loadings/review` | Loading review validation endpoint |
| `/api/loadings` | Loading batch posting endpoint |
| `/health` | Health endpoint |
| `/admin/login` | Admin sign-in |
| `/admin/` | Admin dashboard |
| `/admin/api/loadings` | Loading audit list API |
| `/admin/api/loadings/<loading_id>` | Loading audit detail API |
| `/admin/api/loadings/export` | Loading audit Excel export |

## Heroku deployment

The repository contains a `Procfile` and `wsgi.py` for Heroku deployment.

Deploy the application through the configured Heroku GitHub or Git deployment workflow, then set all required config vars:

```bash
heroku config:set DATABASE_URL="..."
heroku config:set ADMIN_PASSWORD="..."
```

Set the SAP and Microsoft Graph / SharePoint configuration variables in Heroku as well.

Run schema or maintenance scripts as one-off Heroku processes:

```bash
heroku run python scripts/create_schema.py -a YOUR_HEROKU_APP
```

Example:

```bash
heroku run python scripts/add_loading_schema.py -a YOUR_HEROKU_APP
```

## Loading audit export

From the Admin Dashboard → Loadings tab, select **Export Excel**.

The loading export includes:

- Loading ID
- Status
- Created and completed timestamps
- Source location
- Destination
- Shipment / container reference
- Operator
- Device ID
- Override state
- Override reason
- Header item count
- Item sequence
- Handling unit
- Indus part
- Customer part
- Pallet quantity
- Resolution
- Fallback STOCK_IN event
- STOCK_OUT event

Handling units are intentionally written as Excel text to prevent long HU values from being converted to scientific notation or rounded.

## Resetting loading test data

After testing Loading mode, loading-audit records can be reset without deleting warehouse movement history.

The intended loading-only reset script is:

```text
scripts/reset_loading_records.py
```

It must delete data in this order:

```text
1. loading_items
2. loading_headers
```

This order is required because each `loading_items.loading_id` record references `loading_headers.loading_id`.

A correct loading-only reset must not delete or alter:

- `movements`
- `pallet_status`
- `ivt_scans`
- `bdr_scans`
- Database schema
- SAP data
- SharePoint data

Run the script once through Heroku:

```bash
heroku run python scripts/reset_loading_records.py -a YOUR_HEROKU_APP
```

Before running a reset in a production-like environment, export the Loading Audit data if you need to preserve the completed test records.

Important: clearing loading records does not reverse the STOCK_OUT movement records previously posted by those loadings. Existing test HUs may therefore remain recorded as out of the warehouse. Use valid in-stock HUs for new production loading operations.

## Existing maintenance scripts

The `scripts/` folder contains database setup, migration, repair, and maintenance utilities.

Examples include:

| Script | Purpose |
|---|---|
| `create_schema.py` | Create baseline PostgreSQL schema |
| `add_loading_schema.py` | Add loading header and item tables |
| `add_loading_shipment_reference.py` | Add shipment / container reference support |
| `add_hu_metadata_columns.py` | Add HU metadata fields |
| `add_indexes.py` | Add database indexes |
| `migrate_excel_to_pg.py` | Migrate legacy Excel data to PostgreSQL |
| `migrate_pallet_qty_to_int.py` | Migrate pallet quantity storage |
| `reconcile_pallet_status.py` | Rebuild or reconcile pallet statuses |
| `backfill_missing_stock_in.py` | Backfill missing STOCK_IN records where applicable |
| `repair_crossloc_stock_out.py` | Repair cross-location stock-out records |
| `delete_movement.py` | Targeted movement deletion utility |
| `reset_db.py` | Database reset utility |

Do not run repair, deletion, reset, or migration scripts against production without reviewing the script and confirming its effect on the target database.

## Operational safeguards

The application includes several safeguards intended to protect operational integrity:

- PostgreSQL transactions for grouped database work.
- Connection pooling through `ThreadedConnectionPool`.
- Idempotency keys for loading operations.
- Unique movement event IDs.
- Loading header and loading-item separation for traceable audit records.
- Customer-code lookup designed to fail safely without blocking warehouse activity.
- Loading review prior to final post.
- Supervisor override tracking and reason capture.
- Loading status tracking.
- Negative-stock detection in dashboard reporting.
- No automatic deletion of movement history during loading-audit reset operations.

## Manual loading test checklist

Before shipping or changing Loading mode, verify:

- A valid HU can be scanned from the selected source location.
- Loading review displays HU, Indus part, quantity, and customer code.
- A reference ID can be entered before confirmation.
- The loading posts successfully.
- A loading header is created.
- A loading item is created for every posted HU.
- The corresponding STOCK_OUT movement is recorded.
- Admin Dashboard → Loadings displays the loading record.
- Loading detail displays customer code for each item.
- Loading detail displays the reference ID.
- Opening a loading detail does not prevent access to other loading records.
- Excel export includes shipment/container reference and customer part.
- Existing movement, pallet-status, stock, and reconciliation features still work after a loading is posted.

The repository includes [`scripts/test_loading_manual.md`](scripts/test_loading_manual.md) for manual loading-flow validation.

## Security notes

- Keep `.env` and Heroku config vars private.
- Do not commit database URLs, SAP credentials, Microsoft Graph secrets, or admin passwords.
- Change the admin password from any development default before production use.
- Limit admin dashboard access to authorized personnel.
- Treat maintenance scripts as operational tools requiring review and approval.
- Export data before running destructive cleanup or reset scripts.

## Troubleshooting

### Admin dashboard reports database error

Confirm that:

```bash
heroku config:get DATABASE_URL -a YOUR_HEROKU_APP
```

returns the expected PostgreSQL connection string and that the database is reachable.

### Customer code is blank

Customer codes depend on the SharePoint Open Order Report lookup.

Check:

- Microsoft Graph tenant, client ID, and client secret configuration.
- SharePoint site ID configuration.
- Open Order Report workbook filename configuration.
- The expected Indus part value in the workbook.
- The SharePoint application's permissions.

Customer-code lookup failure should not prevent warehouse scanning or loading posting.

### Loading detail is missing a reference ID

The reference field is stored as `shipment_reference` on the loading header. Confirm the value was entered before posting and that the loading schema migration for shipment reference has been applied.

### Loading reset fails with a foreign-key error

Delete `loading_items` before `loading_headers`.

Do not delete headers first, because the item records reference their parent loading record.

### A previously tested HU cannot be loaded again

Deleting loading audit records does not reverse movement history. If the HU was posted as STOCK_OUT during testing, it may remain out of warehouse according to the movement and pallet-status records. Use a currently in-stock HU or perform the appropriate operational stock-in process.

## License

No license is currently defined in this repository.
