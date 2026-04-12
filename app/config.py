"""
Application configuration — credentials hardcoded like Indus-Open-Order-Report-CUST.py
"""


class Config:
    SECRET_KEY = 'indus-warehouse-tracker-2026'
    DEBUG = False

    # Microsoft Entra App Registration (same as Open Order Report script)
    GRAPH_TENANT_ID = 'ed97e9bb-e119-4bd4-ab00-307d64bdf908'
    GRAPH_CLIENT_ID = 'a2239be7-12cd-442d-983f-ea7e316ef767'
    GRAPH_CLIENT_SECRET = 'sho8Q~sanlYEX3SKXjYJb0NSXL.gPRpdi63-tcmp'

    # SharePoint Indus_USA site ID
    SHAREPOINT_SITE_ID = '201c767f-23cc-4eff-b76c-47a275706531,094b21f9-6ed4-47c3-a751-3b217a18b98e'

    # Workbook filename in Shared Documents root
    WORKBOOK_FILENAME = 'Indus Warehouse Stock Movement.xlsx'

    # Worksheet names (must match workbook exactly)
    SHEET_MOVEMENT_LOG = 'Movement_Log'
    SHEET_PALLET_STATUS = 'Pallet_Status'
    SHEET_ERROR_LOG = 'Error_Log'
    SHEET_DEVICE_NAMES = 'Device_Names'
    # Master_Parts sheet exists but is NOT used — all data comes from scanning the label

    # Factory floor auth
    SUPERVISOR_PIN = '9999'
    ADMIN_USERNAME = 'admin'
    ADMIN_PASSWORD = 'indus2026'

    # Timeouts
    GRAPH_REQUEST_TIMEOUT = 30
    GRAPH_MAX_RETRIES = 3

    APP_NAME = 'Indus Warehouse Tracker'
    APP_VERSION = '2.0.0'
