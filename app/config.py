"""
All config from Heroku config vars. No secrets hardcoded.
"""
import os


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-change-me-in-production')
    DEBUG = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'

    # Microsoft Entra App Registration
    GRAPH_TENANT_ID = os.environ.get('GRAPH_TENANT_ID', '')
    GRAPH_CLIENT_ID = os.environ.get('GRAPH_CLIENT_ID', '')
    GRAPH_CLIENT_SECRET = os.environ.get('GRAPH_CLIENT_SECRET', '')

    # SharePoint — composite "siteId,webId"
    SHAREPOINT_SITE_ID = os.environ.get(
        'SHAREPOINT_SITE_ID',
        '201c767f-23cc-4eff-b76c-47a275706531,094b21f9-6ed4-47c3-a751-3b217a18b98e'
    )
    # Workbook filename relative to Shared Documents root
    WORKBOOK_FILENAME = os.environ.get(
        'WORKBOOK_FILENAME',
        'Indus Warehouse Stock Movement.xlsx'
    )

    # Worksheet names (must match workbook exactly)
    SHEET_MASTER_PARTS = 'Master_Parts'
    SHEET_MOVEMENT_LOG = 'Movement_Log'
    SHEET_PALLET_STATUS = 'Pallet_Status'
    SHEET_ERROR_LOG = 'Error_Log'

    # Factory floor auth
    SUPERVISOR_PIN = os.environ.get('SUPERVISOR_PIN', '9999')
    ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'change-me')

    # Timeouts
    GRAPH_REQUEST_TIMEOUT = int(os.environ.get('GRAPH_REQUEST_TIMEOUT', '30'))
    GRAPH_MAX_RETRIES = int(os.environ.get('GRAPH_MAX_RETRIES', '3'))

    APP_NAME = 'Indus Warehouse Tracker'
    APP_VERSION = '1.0.0'
