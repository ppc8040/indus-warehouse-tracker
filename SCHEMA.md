# Excel Workbook Schema — Indus Warehouse Stock Movement.xlsx

Located at SharePoint: `indusair.sharepoint.com/sites/Indus_USA/Shared Documents/Indus Warehouse Stock Movement.xlsx`

**No named tables** — use worksheet range operations (same pattern as Indus-Open-Order-Report-CUST.py).

## Sheet: Master_Parts (7 columns)
| Col | Header         | Notes                                     |
|-----|---------------|-------------------------------------------|
| A   | Handling_Unit  | Primary key. Scanned barcode value.       |
| B   | Indus_Part     | Indus part number                         |
| C   | Customer_Part  | Customer part number                      |
| D   | Pallet_Qty     | Quantity on pallet                        |
| E   | Job_No         | Job/production order number               |
| F   | Description    | Part description                          |
| G   | Active_Flag    | "YES" or "NO" — inactive pallets blocked  |

## Sheet: Movement_Log (12 columns)
| Col | Header             | Notes                                        |
|-----|--------------------|----------------------------------------------|
| A   | Event_ID           | UUID generated per event                     |
| B   | Timestamp_UTC      | ISO 8601 UTC timestamp                       |
| C   | Handling_Unit      | The scanned HU                               |
| D   | Indus_Part         | Copied from Master_Parts at scan time        |
| E   | Customer_Part      | Copied from Master_Parts at scan time        |
| F   | Pallet_Qty         | Copied from Master_Parts at scan time        |
| G   | Job_No             | Copied from Master_Parts at scan time        |
| H   | Movement_Type      | STOCK_IN or STOCK_OUT                        |
| I   | Operator_ID        | Worker identifier                            |
| J   | Device_ID          | Browser/device fingerprint                   |
| K   | Sync_Status        | SYNCED or PENDING                            |
| L   | Validation_Status  | VALID, OVERRIDE, REJECTED                    |

## Sheet: Pallet_Status (5 columns)
| Col | Header              | Notes                                    |
|-----|---------------------|------------------------------------------|
| A   | Handling_Unit       | Primary key. Links to Master_Parts.      |
| B   | Current_Status      | IN_WAREHOUSE or OUT_OF_WAREHOUSE         |
| C   | Last_Event_ID       | Links to Movement_Log.Event_ID           |
| D   | Last_Movement_Type  | STOCK_IN or STOCK_OUT                    |
| E   | Last_Movement_Time  | ISO 8601 UTC timestamp                   |

## Sheet: Error_Log (7 columns)
| Col | Header          | Notes                                        |
|-----|-----------------|----------------------------------------------|
| A   | Error_ID        | UUID                                         |
| B   | Timestamp_UTC   | ISO 8601 UTC                                 |
| C   | Handling_Unit   | The HU involved (or empty)                   |
| D   | Stage           | LOOKUP, MOVEMENT, SYNC, OVERRIDE, SYSTEM     |
| E   | Error_Code      | Machine-readable code                        |
| F   | Error_Message   | Human-readable detail (max 500 chars)        |
| G   | Resolved_Flag   | NO or YES                                    |

## Graph API URL Pattern (from reference code)
```
Base: https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root:/{filename}:/workbook
Session: .../workbook/createSession  (POST, persistChanges: true)
Close:   .../workbook/closeSession   (POST)
Worksheets: .../workbook/worksheets('{sheet_name}')
UsedRange: .../worksheets('{sheet_name}')/usedRange
Range:   .../worksheets('{sheet_name}')/range(address='{A1:G100}')
```

## Auth (from reference code)
- MSAL ConfidentialClientApplication
- acquire_token_silent first, then acquire_token_for_client
- Scopes: ['https://graph.microsoft.com/.default']
- Tenant: ed97e9bb-e119-4bd4-ab00-307d64bdf908
- Site ID: 201c767f-23cc-4eff-b76c-47a275706531,094b21f9-6ed4-47c3-a751-3b217a18b98e
