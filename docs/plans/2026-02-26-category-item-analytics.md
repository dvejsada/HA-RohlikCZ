# Category & Item Spending Analytics Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enrich stored orders with item-level details and add two new category breakdown sensors (this year + all time) with spent, units, and avg_unit_price per level-1 category.

**Architecture:** Extend OrderStore (v1→v2) to store item details per order. Add `get_order_detail` API method for `/api/v3/orders/{orderId}`. Modify `fetch_order_history` to also enrich. Auto-enrich new orders during regular polls. Two new sensors expose category data as attributes for ApexCharts dashboards. No existing sensors modified.

**Tech Stack:** Python, Home Assistant custom component APIs, `requests`, JSON file storage

---

## Context

### What exists (Phase 1)

- `OrderStore` in `hub.py` stores `{order_id: {date, amount}}` in `.storage/rohlikcz_{user_id}_orders.json` (version 1)
- 483 orders stored, ~38KB, spanning 2017–2026
- `YearlySpent` and `AllTimeSpent` sensors read from the store
- `fetch_order_history` service backfills all historical orders
- Regular 10-min poll fetches 50 most recent delivered orders

### What the API provides

- **Detail endpoint** (`/api/v3/orders/{orderId}`): full order with items array. Each item includes product ID, name, quantity, price, brand, and `categories` array with `{id, name, level}` hierarchy.
- Level 1 = mid-level grouping (e.g. "Pečivo", "Mléčné výrobky"). ~15-30 distinct categories.
- **Cost**: 1 API call per order. 483 orders × 200ms = ~100 seconds one-time. Storage grows to ~1.5-3MB.

### Non-breaking guarantees

- ALL existing sensors untouched (MonthlySpent, YearlySpent, AllTimeSpent, etc.)
- ALL existing services untouched
- Store v1→v2 migration is additive (just bumps version, existing data unchanged)
- Orders without `items` key are "unenriched" and silently skipped by analytics
- `fetch_order_history` response is a superset of the old response

---

## Store Schema v2

```json
{
  "version": 2,
  "user_id": "1234567",
  "tracking_since": "2026-02-26T13:01:11+01:00",
  "orders": {
    "1119530344": {
      "date": "2026-02-23",
      "amount": 997.0,
      "items": [
        {
          "id": 12345,
          "name": "Polotučné mléko 1,5% 1l",
          "quantity": 2,
          "price": 28.90,
          "brand": "Madeta",
          "category": "Mléčné výrobky",
          "category_id": 300015
        }
      ]
    },
    "1119428209": {
      "date": "2026-02-22",
      "amount": 430.33
    }
  }
}
```

Orders without `"items"` = unenriched. All existing methods (`yearly_total`, `alltime_total`, etc.) use only `date` and `amount` — completely unchanged.

---

## New Sensors

### `sensor.rohlik_categories_this_year`
- State: number of level-1 categories with spending this year
- Attribute `categories` (sorted by spent desc):
```json
[
  {"name": "Mléčné výrobky", "spent": 15230.50, "units": 612, "avg_unit_price": 24.89},
  {"name": "Pečivo", "spent": 12100.00, "units": 890, "avg_unit_price": 13.60}
]
```
- Attribute `enriched_orders`: count of enriched orders this year
- Attribute `total_orders`: count of all orders this year

### `sensor.rohlik_categories_all_time`
- Same structure, all time

### Dashboard usage (ApexCharts)
```yaml
# Donut: where does my money go?
chart_type: donut
data_generator: |
  return entity.attributes.categories.slice(0,5).map(c => [c.name, c.spent]);

# Bar: what do I buy most?
chart_type: bar
data_generator: |
  return entity.attributes.categories.slice(0,5).map(c => [c.name, c.units]);

# Bar: most expensive per unit?
chart_type: bar
data_generator: |
  return entity.attributes.categories.slice(0,10).map(c => [c.name, c.avg_unit_price]);
```

---

### Task 1: Probe API — confirm order detail structure

**Files:**
- Modify: `custom_components/rohlikcz/rohlik_api.py` (add `get_order_detail` method after `get_delivered_orders_page`)
- Modify: `custom_components/rohlikcz/services.py` (temporary probe service)

**Goal:** Fetch one real order's detail to confirm field names before building anything.

**Step 1: Add `get_order_detail` to API**

Add after `get_delivered_orders_page` in `rohlik_api.py`:

```python
async def get_order_detail(self, session, order_id: str) -> dict | None:
    """Fetch detailed order info including items for a single order."""
    url = f"{BASE_URL}/api/v3/orders/{order_id}"
    try:
        response = await self._run_in_executor(session.get, url)
        response.raise_for_status()
        return response.json()
    except RequestException as err:
        _LOGGER.error(f"Error fetching order detail for {order_id}: {err}")
        return None
```

**Step 2: Add temporary probe service**

Add to `services.py` a temporary `probe_order_detail` service. See implementation in code (will be removed after probe).

**Step 3: Deploy to HA, restart, call probe**

```bash
cp custom_components/rohlikcz/rohlik_api.py /Volumes/config/custom_components/rohlikcz/
cp custom_components/rohlikcz/services.py /Volumes/config/custom_components/rohlikcz/
```

Call via MCP:
```
ha_call_service("rohlikcz", "probe_order_detail",
    data={"config_entry_id": "<entry_id>", "order_id": "1119530344"},
    return_response=True)
```

**Step 4: Document actual field names, remove probe service**

Record: items key name, product ID field, name field, quantity field, price structure, categories array structure, levels available.

**Step 5: Commit (API method only, probe removed)**

```bash
git add custom_components/rohlikcz/rohlik_api.py
git commit -m "feat: add get_order_detail API method for item-level data"
```

---

### Task 2: Upgrade OrderStore to v2 with item storage and aggregation

**Files:**
- Modify: `custom_components/rohlikcz/hub.py` (OrderStore class)

**Step 1: Add v1→v2 migration in `load()`**

After loading JSON, if version < 2, bump to 2 and save. No data changes needed.

**Step 2: Add `_extract_items` static method**

Normalize item data from API response — handle field name variations (id/productId, name/productName, etc.). Extract level-1 category. Return list of `{id, name, quantity, price, brand, category, category_id}`.

**Step 3: Add enrichment methods**

- `enrich_order(order_id, detail)` → extract items and add to order entry
- `unenriched_order_ids` property → list of order IDs without items
- `enriched_count` property

**Step 4: Add aggregation methods**

- `category_totals(year=None)` → returns `[{"name": "Dairy", "spent": 5000, "units": 200, "avg_unit_price": 25.0}]` sorted by spent desc
- Uses only enriched orders, skips unenriched

**Step 5: Verify syntax, commit**

```bash
git add custom_components/rohlikcz/hub.py
git commit -m "feat: upgrade OrderStore to v2 with item storage and category aggregation"
```

---

### Task 3: Wire enrichment into hub and API

**Files:**
- Modify: `custom_components/rohlikcz/rohlik_api.py` (add `enrich_orders` batch method)
- Modify: `custom_components/rohlikcz/hub.py` (modify `fetch_full_order_history`, add auto-enrich in `async_update`)
- Modify: `custom_components/rohlikcz/services.py` (update service handler return)

**Step 1: Add `enrich_orders` to API**

Batch method that takes list of order IDs, fetches detail for each with 200ms rate limit, returns `{order_id: detail}` dict.

**Step 2: Modify `fetch_full_order_history` in hub**

After fetching order list (existing), also enrich all unenriched orders. Return dict: `{total_orders, new_orders, enriched_orders, unenriched_remaining}`.

**Step 3: Add auto-enrich in `async_update`**

After `process_orders` detects new orders, fetch their details (0-1 API calls per poll).

**Step 4: Update service handler**

Pass through the new dict from `fetch_full_order_history` (superset of old response).

**Step 5: Verify syntax, commit**

```bash
git add custom_components/rohlikcz/rohlik_api.py custom_components/rohlikcz/hub.py custom_components/rohlikcz/services.py
git commit -m "feat: wire order enrichment into fetch and poll flows"
```

---

### Task 4: Add category breakdown sensors

**Files:**
- Modify: `custom_components/rohlikcz/sensor.py` (add 2 new sensor classes)
- Modify: `custom_components/rohlikcz/const.py` (add icon)
- Modify: `custom_components/rohlikcz/translations/en.json`
- Modify: `custom_components/rohlikcz/translations/cs.json`

**Step 1: Add icon to const.py**

```python
ICON_CATEGORY_SPENDING = "mdi:shape"
```

**Step 2: Add `CategorySpendingYearly` sensor**

- `_attr_translation_key = "categories_this_year"`
- State: number of categories with spending
- Attribute `categories`: sorted list of `{name, spent, units, avg_unit_price}` for all level-1 categories
- Attributes: `enriched_orders`, `total_orders`, `year`

**Step 3: Add `CategorySpendingAllTime` sensor**

Same structure, no year filter.

**Step 4: Register in `async_setup_entry` entity list**

Append after AllTimeSpent.

**Step 5: Add translations**

EN: "Categories This Year", "Categories All Time"
CS: "Kategorie tento rok", "Kategorie celkem"

**Step 6: Verify syntax, commit**

```bash
git add custom_components/rohlikcz/sensor.py custom_components/rohlikcz/const.py custom_components/rohlikcz/translations/
git commit -m "feat: add category breakdown sensors for yearly and all-time"
```

---

### Task 5: Tests

**Files:**
- Modify: `tests/test_order_store.py`

**Step 1: Add tests for item extraction, category aggregation, v2 migration**

- Test `_extract_items` with various API response formats
- Test `category_totals` with mixed enriched/unenriched orders
- Test v1→v2 migration preserves data
- Test incremental enrichment

**Step 2: Run tests, commit**

```bash
python3 tests/test_order_store.py
git add tests/test_order_store.py
git commit -m "test: add v2 item extraction and category aggregation tests"
```

---

### Task 6: Deploy, backfill, and verify

**Step 1: Copy all files to HA, restart**

**Step 2: Verify existing sensors still work (no regression)**

**Step 3: Run `fetch_order_history` to enrich all 483 orders (~100 seconds)**

**Step 4: Verify new category sensors show data**

**Step 5: Verify store file has items**

**Step 6: Commit final state**

---

## Summary

| Task | What | Touches existing? |
|------|------|-------------------|
| 1 | Probe API + `get_order_detail` method | No — new method only |
| 2 | OrderStore v2 + aggregation | No — additive migration, new methods |
| 3 | Wire enrichment into flows | Minimal — `fetch_full_order_history` returns superset |
| 4 | Two new category sensors | No — new classes appended |
| 5 | Tests | No |
| 6 | Deploy + verify | No |
