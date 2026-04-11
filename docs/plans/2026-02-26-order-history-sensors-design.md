# Order History Sensors - Design Doc

**Date:** 2026-02-26
**Branch:** `feat/order-history-sensors`
**Upstream issue:** [dvejsada/HA-RohlikCZ#52](https://github.com/dvejsada/HA-RohlikCZ/issues/52)
**Fork:** [kwaczek/HA-RohlikCZ](https://github.com/kwaczek/HA-RohlikCZ)

## Goal

Add yearly and all-time spending sensors to the Rohlik HA integration. This is Phase 1 — the foundation for future per-product and per-category breakdowns.

## Current State

- **MonthlySpent** sensor exists, tracks current month only, resets on the 1st
- API fetches `delivered_orders` with `limit=50` on every 600s poll cycle
- Only order-level data (ID, date, total) — no item details
- No persistent storage of order history — data lives in memory, only the monthly total survives restarts via `RestoreEntity`

## What We're Adding

### New Sensors

1. **YearlySpent** — total spent this calendar year
   - `SensorStateClass.TOTAL`, resets Jan 1
   - Attributes: `order_count`, `average_order_value`, `current_year`

2. **AllTimeSpent** — cumulative total across all tracked orders
   - `SensorStateClass.TOTAL_INCREASING`
   - Attributes: `order_count`, `average_order_value`, `first_order_date`, `tracking_since`

### Local Order Store

JSON file at `/config/.storage/rohlikcz_{user_id}_orders.json`:

```json
{
  "version": 1,
  "user_id": "<user_id>",
  "tracking_since": "2026-02-26T12:00:00+01:00",
  "orders": {
    "1119530344": {"date": "2026-02-25", "amount": 1523.50},
    "1119428209": {"date": "2026-02-22", "amount": 987.00}
  }
}
```

- Written to HA's `.storage/` directory (standard location for integration data)
- Updated incrementally — only new orders added on each poll
- Survives restarts, updates, and reinstalls

### New Service: `rohlikcz.fetch_order_history`

One-time backfill of all historical orders:
- Paginates through `/api/v3/orders/delivered?offset=N&limit=50`
- Stores every order's ID, date, and total amount
- Rate-limited (200ms between pages)
- Logs progress

### New API Method

`get_delivered_orders_page(session, offset, limit)` in `rohlik_api.py`:
- Single authenticated call to the delivered orders endpoint
- Returns list of order dicts
- Used by both the regular poll (offset=0, limit=50) and the backfill service

## Architecture

```
                  ┌─────────────────┐
                  │  rohlik_api.py   │
                  │  (API calls)     │
                  └────────┬────────┘
                           │ delivered orders
                           ▼
                  ┌─────────────────┐
                  │    hub.py        │
                  │  (data + store)  │
                  │                  │
                  │  order_store:    │
                  │  load/save JSON  │
                  │  process_orders  │
                  └────────┬────────┘
                           │ hub.data + hub.order_store
                           ▼
              ┌────────────┴────────────┐
              │                         │
     ┌────────┴──────┐       ┌─────────┴───────┐
     │  YearlySpent   │       │  AllTimeSpent    │
     │  sensor         │       │  sensor          │
     │  (reads store)  │       │  (reads store)   │
     └────────────────┘       └─────────────────┘
```

## Files Changed

| File | Change |
|------|--------|
| `rohlik_api.py` | Add `get_delivered_orders_page(session, offset, limit)` method |
| `hub.py` | Add `OrderStore` class (load/save JSON, process new orders). Add `fetch_all_order_history()` for backfill |
| `sensor.py` | Add `YearlySpent` and `AllTimeSpent` sensor classes |
| `services.py` | Register `fetch_order_history` service |
| `services.yaml` | Define `fetch_order_history` service schema |
| `const.py` | Add new icons and constants |
| `translations/en.json` | Add sensor names |
| `translations/cs.json` | Add Czech sensor names |

## Design Decisions

1. **JSON in `.storage/` vs SQLite** — JSON is simpler, human-readable, and sufficient for ~500 orders. `.storage/` is HA's standard location for integration data.

2. **Separate store vs extending RestoreEntity** — RestoreEntity stores attributes in HA's state machine, which isn't designed for growing datasets (500+ order IDs). A separate file keeps the state machine clean.

3. **Incremental sync vs full fetch** — On every poll, we process the 50 delivered orders already being fetched. New ones get added to the store. The backfill service is a one-time operation for historical data.

4. **No item-level data yet** — Phase 1 only stores order totals. Phase 2 will add per-item data (requires additional API calls per order). The store schema has `version: 1` to allow migration.

## Testing Strategy

- Standalone Python script to test API pagination and order processing logic
- Unit tests for OrderStore (load, save, deduplication, yearly/alltime aggregation)
- Deploy to live HA only after local tests pass

## Phase 2 (future)

- Fetch order details (items, quantities, prices) via `/api/v3/orders/{id}`
- Fetch product categories via `/api/v1/products/{id}/categories`
- Cache product→category mapping
- Add category breakdown sensors
