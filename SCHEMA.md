# Excel Workbook Schema — Indus Warehouse Stock Movement.xlsx

Located at SharePoint: `indusair.sharepoint.com/sites/Indus_USA/Shared Documents/Indus Warehouse Stock Movement.xlsx`

**No named tables** — use worksheet range operations (same pattern as Indus-Open-Order-Report-CUST.py).

## Sheet: Movement_Log (13 columns)
| Col | Header             | Notes                                                       |
|-----|--------------------|-------------------------------------------------------------|
| A   | Event_ID           | UUID generated per event                                    |
| B   | Timestamp_UTC      | Stored in Dubai display time format                         |
| C   | Handling_Unit      | The scanned HU                                              |
| D   | Indus_Part         | Captured from the scanned label                             |
| E   | Customer_Part      | Captured from the scanned label                             |
| F   | Pallet_Qty         | Captured from the scanned label                             |
| G   | Job_No             | Captured from the scanned label if supplied                 |
| H   | Movement_Type      | STOCK_IN or STOCK_OUT                                       |
| I   | Operator_ID        | Worker identifier                                           |
| J   | Device_ID          | Browser/device fingerprint                                  |
| K   | Sync_Status        | SYNCED or PENDING                                           |
| L   | Validation_Status  | VALID or OVERRIDE                                           |
| M   | Location           | One of: M4, M5, M6, M4_STORE, M16_STORE                    |

## Sheet: Pallet_Status (8 columns)
| Col | Header              | Notes                                                                        |
|-----|---------------------|------------------------------------------------------------------------------|
| A   | Handling_Unit       | Primary key                                                                  |
| B   | Current_Status      | IN_WAREHOUSE or OUT_OF_WAREHOUSE                                             |
| C   | Last_Event_ID       | Links to Movement_Log.Event_ID                                               |
| D   | Last_Movement_Type  | STOCK_IN or STOCK_OUT                                                        |
| E   | Last_Movement_Time  | Stored in Dubai display time format                                          |
| F   | Indus_Part          | Last scanned Indus Part, used in repeat STOCK_IN checks                      |
| G   | Pallet_Qty          | Last scanned pallet qty, used in repeat STOCK_IN checks                      |
| H   | Last_Location       | Location of the most recent movement (IN or OUT). Used for STOCK_OUT lock.   |

## Sheet: Error_Log (7 columns)
| Col | Header          | Notes                                        |
|-----|-----------------|----------------------------------------------|
| A   | Error_ID        | UUID                                         |
| B   | Timestamp_UTC   | Stored in Dubai display time format          |
| C   | Handling_Unit   | The HU involved (or empty)                   |
| D   | Stage           | LOOKUP, MOVEMENT, SYNC, OVERRIDE, SYSTEM     |
| E   | Error_Code      | Machine-readable code                        |
| F   | Error_Message   | Human-readable detail (max 500 chars)        |
| G   | Resolved_Flag   | NO or YES                                    |

## Validation Rules Backed by Schema

### Duplicate Detection
- Repeated `STOCK_IN` is blocked only when the same `Handling_Unit`, `Indus_Part`, and `Pallet_Qty` are already in the last pallet state.
- Same `Handling_Unit` with a different `Indus_Part` or `Pallet_Qty` is treated as a new pallet run and allowed.
- `STOCK_OUT` is blocked when the last state is already `STOCK_OUT`.
- `STOCK_OUT` is blocked when the handling unit has never been stocked in, unless supervisor override is used.
- `Location` is intentionally excluded from the duplicate fingerprint — the state transition check already prevents double STOCK_INs regardless of location.

### Location Rules
- Every `STOCK_IN` and `STOCK_OUT` must include a valid location: `M4`, `M5`, `M6`, `M4_STORE`, or `M16_STORE`.
- When a pallet is `STOCK_OUT`, the selected location **must match** `Last_Location` in Pallet_Status (the location it was last `STOCK_IN` at). This prevents out-of-location movements.
- After any `STOCK_OUT`, the pallet is free and may be `STOCK_IN` at any location on the next movement.
- `Last_Location` in Pallet_Status always reflects the location of the most recent movement (whether `STOCK_IN` or `STOCK_OUT`).
- Supervisor override bypasses the location lock entirely.

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
