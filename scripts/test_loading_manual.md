# Loading Mode Manual Test Checklist

Run only against a safe development database.

## Before starting

1. Set `DATABASE_URL` to development.
2. Run `python scripts/add_loading_schema.py`.
3. Start the app.
4. Use a clean browser profile or clear only `sessionStorage` for the app origin.

## Normal scanner regression

- [ ] Loading Mode OFF.
- [ ] Submit normal STOCK_IN.
- [ ] Confirm exactly one normal STOCK_IN movement.
- [ ] Submit normal STOCK_OUT.
- [ ] Confirm exactly one normal STOCK_OUT movement.
- [ ] Confirm normal scanner request still uses `/api/movement`.
- [ ] Confirm no new loading header/item exists for normal movements.

## Existing HU success

- [ ] Create or locate HU currently STOCK_IN at M4.
- [ ] Add it to Loading Mode with source M4.
- [ ] Review reports `EXISTING_STOCK_IN`.
- [ ] Confirm final post.
- [ ] Confirm one `STOCK_OUT` movement to `LOADING`.
- [ ] Confirm loading header is `COMPLETED`.
- [ ] Confirm loading item references the outbound event.
- [ ] Confirm pallet status becomes `OUT_OF_WAREHOUSE`.

## Existing HU hard block

- [ ] Use HU currently STOCK_OUT.
- [ ] Add to Loading Mode.
- [ ] Review rejects it.
- [ ] Repeat with supervisor fields populated.
- [ ] Confirm final post still rejects it.
- [ ] Confirm zero new movements and zero completed loading records.

## Missing HU override

- [ ] Use a valid-format HU absent from `pallet_status`.
- [ ] Add it to the loading queue.
- [ ] Review says supervisor approval is required.
- [ ] Post without supervisor PIN and reason: reject.
- [ ] Post with incorrect PIN: reject.
- [ ] Post with valid PIN and reason: success.
- [ ] Confirm exactly two movements:
  - one STOCK_IN at source location, validation status OVERRIDE;
  - one STOCK_OUT from source location to LOADING, validation status OVERRIDE.
- [ ] Confirm loading item has `MISSING_HU_FALLBACK`.
- [ ] Confirm both event IDs appear in the loading item.

## Duplicate and persistence

- [ ] Add same HU twice in the scanner queue: second add is blocked.
- [ ] Refresh browser: queue remains.
- [ ] Navigate away and back: queue remains.
- [ ] Remove one HU: only that HU is removed.
- [ ] Clear queue: confirmation appears and queue clears only after confirmation.
- [ ] Failed posting preserves the queue.

## Atomicity

- [ ] Prepare a queue with one valid HU and one invalid existing HU.
- [ ] Post the batch.
- [ ] Confirm final posting fails.
- [ ] Confirm no outgoing movement was written for the earlier valid HU.
- [ ] Confirm no loading header is left as COMPLETED.

## Idempotency

- [ ] Submit final request once and note `loading_id`.
- [ ] Repeat identical POST with the same idempotency key.
- [ ] Confirm response is successful with `replayed: true`.
- [ ] Confirm no additional STOCK_OUT movements were created.
- [ ] Double-click final confirmation in browser.
- [ ] Confirm only one completed loading exists.

## Admin audit

- [ ] Open Admin Dashboard → Loadings.
- [ ] Confirm completed batch is visible.
- [ ] Filter Completed.
- [ ] Open detail.
- [ ] Confirm operator, source, destination, count, timestamps, override status, reason, HU items, and event IDs appear.

## PWA deploy

- [ ] Deploy to Heroku.
- [ ] Open scanner app.
- [ ] Confirm service worker cache name changes after deployment.
- [ ] Hard refresh once.
- [ ] Confirm Loading Mode assets load.
- [ ] Confirm API requests remain network requests, not cached writes.
