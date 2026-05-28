"""
Application configuration.
"""
import os


class Config:
    SECRET_KEY = 'indus-warehouse-tracker-2026'
    DEBUG = False

    # ── PostgreSQL ───────────────────────────────────────────────────────────
    DATABASE_URL = os.environ.get(
        'DATABASE_URL',
        'postgresql://localhost/indus_warehouse_tracker',
    )
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

    # ── Auth ─────────────────────────────────────────────────────────────────
    SUPERVISOR_PIN  = '9999'
    ADMIN_USERNAME  = 'admin'
    ADMIN_PASSWORD  = 'indus2026'

    # ── Session ──────────────────────────────────────────────────────────────
    SESSION_COOKIE_HTTPONLY  = True
    SESSION_COOKIE_SAMESITE  = 'Lax'
    PERMANENT_SESSION_LIFETIME = 28800  # 8 hours

    # ── App identity ─────────────────────────────────────────────────────────
    APP_NAME    = 'Indus Warehouse Tracker'
    APP_VERSION = '2.0.0'

    # ── Microsoft Graph / SharePoint ─────────────────────────────────────────
    GRAPH_TENANT_ID      = 'ed97e9bb-e119-4bd4-ab00-307d64bdf908'
    GRAPH_CLIENT_ID      = 'a2239be7-12cd-442d-983f-ea7e316ef767'
    GRAPH_CLIENT_SECRET  = 'sho8Q~sanlYEX3SKXjYJb0NSXL.gPRpdi63-tcmp'
    SHAREPOINT_SITE_ID   = ('201c767f-23cc-4eff-b76c-47a275706531,'
                             '094b21f9-6ed4-47c3-a751-3b217a18b98e')
    WORKBOOK_FILENAME     = 'Indus Warehouse Stock Movement.xlsx'
    WORKBOOK_FILENAME_OOR = 'Indus Open Order Report New.xlsx'
    SHEET_MOVEMENT_LOG   = 'Movement_Log'
    SHEET_PALLET_STATUS  = 'Pallet_Status'
    SHEET_ERROR_LOG      = 'Error_Log'
    SHEET_DEVICE_NAMES   = 'Device_Names'
    SHEET_IVT_LINE       = 'IVT_Line'
    GRAPH_REQUEST_TIMEOUT = 30
    GRAPH_MAX_RETRIES     = 3
