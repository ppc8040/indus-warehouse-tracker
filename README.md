# Indus Warehouse Tracker

The primary warehouse management web application for Indus International FZC. A Flask-based system that tracks all inventory movements — goods receipts, inter-store transfers, and FG releases — backed by Microsoft SharePoint Excel (via Graph API) and deployed on Heroku.

## What It Does

- **Goods Receipt (GR)** — log incoming stock with SAP movement type 101; records part number, quantity, batch, storage location, and timestamp
- **Store Transfers** — log internal material movements between warehouse locations (M4, M5, M6, M16_STORE, etc.)
- **FG Releases** — record finished goods releases to customers against open POs
- **Live Inventory View** — real-time stock-on-hand table aggregated across all storage locations
- **SharePoint Excel Backend** — all transactions written directly to `Indus Ultimate Stock Recon.xlsx` on SharePoint via Microsoft Graph API (no separate database)
- **Microsoft 365 SSO** — login via MSAL (Azure AD OAuth2)
- **Timestamp fix (April 2026)** — timestamps stored as Excel serial numbers (Dubai local time) so Excel date filters and sorting work natively

## Architecture

```
User (Browser)
    ↓ MSAL SSO
Flask App (Heroku)
    ↓ Microsoft Graph API
SharePoint Excel
└── Indus Ultimate Stock Recon.xlsx
    ├── GR Tab
    ├── Transfers Tab
    ├── Releases Tab
    └── Physical FG Variance Report
```

## Tech Stack

- **Python 3.11 / Flask**
- **Microsoft Graph API** — SharePoint Excel read/write (workbook sessions)
- **MSAL** — Microsoft 365 / Azure AD authentication
- **openpyxl / pandas** — Excel processing
- **Heroku** — deployment (Procfile + `requirements.txt`)

## Relationship to Other Repos

| Repo | Role |
|------|------|
| `indus-warehouse-tracker` | ✅ **This repo** — current production app |
| `new_try` | Portal Release Tracker — FG release management with PO allocation |
| `fg-variance-email` | Automated variance email reporter reading from the same SharePoint file |
| `indus-inventory-deprecated` | ❌ Deprecated predecessor (Heroku + PostgreSQL) |
| `try1` | ❌ Archived prototype |

## Setup (Local)

```bash
pip install -r requirements.txt
```

Set environment variables:

```
CLIENT_ID=...
CLIENT_SECRET=...
TENANT_ID=...
SITE_ID=...
SECRET_KEY=...
```

Run:
```bash
python run.py
```

## Recent Changes

- **April 2026** — `_fmt_timestamp()` now returns an Excel serial float (days since 1899-12-30, Dubai local time) instead of a formatted string, so timestamps render as native Excel datetimes and support date filtering/sorting
- **April 2026** — Timestamp display format set to `D/M/YYYY H:MM` (24h, no seconds, no leading zeros)

---
*Indus International FZC — Internal Warehouse Management System*
