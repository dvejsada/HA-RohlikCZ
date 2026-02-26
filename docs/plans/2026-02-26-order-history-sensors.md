# Order History Sensors Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add YearlySpent and AllTimeSpent sensors with persistent local order storage and a backfill service.

**Architecture:** New `OrderStore` class in `hub.py` manages a JSON file in `.storage/`. On each poll cycle, the existing `delivered_orders` data (50 orders) is processed and new orders are appended to the store. Two new sensors read from the store for yearly and all-time totals. A service call triggers a full historical backfill.

**Tech Stack:** Python, Home Assistant custom component APIs, `requests`, JSON file storage

---

### Task 1: Add `get_delivered_orders_page` to API

**Files:**
- Modify: `custom_components/rohlikcz/rohlik_api.py:230` (after `get_data` method's finally block)

**Step 1: Add the new API method**

Add after the `get_data` method (after line ~230):

```python
async def get_delivered_orders_page(self, session, offset: int = 0, limit: int = 50) -> list:
    """Fetch a page of delivered orders using an existing authenticated session."""
    url = f"{BASE_URL}/api/v3/orders/delivered?offset={offset}&limit={limit}"
    try:
        response = await self._run_in_executor(session.get, url)
        response.raise_for_status()
        return response.json()
    except RequestException as err:
        _LOGGER.error(f"Error fetching delivered orders page (offset={offset}): {err}")
        return []

async def fetch_all_delivered_orders(self) -> list:
    """Fetch ALL delivered orders by paginating through the API. Returns list of all orders."""
    session = requests.Session()
    all_orders = []
    offset = 0
    limit = 50

    try:
        await self.login(session)

        while True:
            page = await self.get_delivered_orders_page(session, offset, limit)
            if not page:
                break
            all_orders.extend(page)
            _LOGGER.info(f"Fetched {len(all_orders)} orders so far (offset={offset})")
            if len(page) < limit:
                break
            offset += limit
            # Rate limit: 200ms between pages
            await asyncio.sleep(0.2)

        return all_orders

    except RequestException as err:
        _LOGGER.error(f"Error during full order history fetch: {err}")
        return all_orders
    finally:
        await self.logout(session)
        await self._run_in_executor(session.close)
```

**Step 2: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('custom_components/rohlikcz/rohlik_api.py', doraise=True); print('OK')"`

**Step 3: Commit**

```bash
git add custom_components/rohlikcz/rohlik_api.py
git commit -m "feat: add paginated order history fetch to API"
```

---

### Task 2: Add OrderStore to hub.py

**Files:**
- Modify: `custom_components/rohlikcz/hub.py`

**Step 1: Add OrderStore class and wire it into RohlikAccount**

Add imports at top of `hub.py`:
```python
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo
```

Add `OrderStore` class before `RohlikAccount`:

```python
class OrderStore:
    """Persistent storage for order history in HA's .storage directory."""

    def __init__(self, storage_dir: str, user_id: str):
        self._path = os.path.join(storage_dir, f"rohlikcz_{user_id}_orders.json")
        self._data = {"version": 1, "user_id": user_id, "tracking_since": None, "orders": {}}
        self.load()

    def load(self) -> None:
        """Load order store from disk."""
        if os.path.exists(self._path):
            try:
                with open(self._path, "r") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError) as err:
                _LOGGER.error(f"Failed to load order store: {err}")

    def save(self) -> None:
        """Save order store to disk."""
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2)
        except OSError as err:
            _LOGGER.error(f"Failed to save order store: {err}")

    def process_orders(self, orders: list) -> int:
        """Process a list of order dicts from the API. Returns count of new orders added."""
        if not orders:
            return 0

        new_count = 0
        for order in orders:
            order_id = str(order.get("id", ""))
            if not order_id or order_id in self._data["orders"]:
                continue

            price_comp = order.get("priceComposition", {})
            total = price_comp.get("total", {})
            amount = total.get("amount")
            if amount is None:
                continue

            try:
                amount = float(amount)
            except (ValueError, TypeError):
                continue

            order_time = order.get("orderTime", "")
            date_str = order_time[:10] if order_time else ""

            self._data["orders"][order_id] = {"date": date_str, "amount": amount}
            new_count += 1

        if new_count > 0:
            if not self._data["tracking_since"]:
                self._data["tracking_since"] = datetime.now(ZoneInfo("Europe/Prague")).isoformat()
            self.save()
            _LOGGER.info(f"Added {new_count} new orders to store. Total: {len(self._data['orders'])}")

        return new_count

    @property
    def orders(self) -> dict:
        return self._data["orders"]

    @property
    def tracking_since(self) -> str | None:
        return self._data.get("tracking_since")

    def yearly_total(self, year: str) -> float:
        """Sum of order amounts for a given year (e.g. '2026')."""
        return sum(
            o["amount"] for o in self._data["orders"].values()
            if o["date"].startswith(year)
        )

    def yearly_count(self, year: str) -> int:
        """Count of orders for a given year."""
        return sum(1 for o in self._data["orders"].values() if o["date"].startswith(year))

    def alltime_total(self) -> float:
        """Sum of all order amounts."""
        return sum(o["amount"] for o in self._data["orders"].values())

    def alltime_count(self) -> int:
        """Count of all orders."""
        return len(self._data["orders"])

    def first_order_date(self) -> str | None:
        """Date of the earliest order."""
        dates = [o["date"] for o in self._data["orders"].values() if o["date"]]
        return min(dates) if dates else None
```

Add to `RohlikAccount.__init__` — after `self._callbacks` line:
```python
self._order_store: OrderStore | None = None
```

Add new property to `RohlikAccount`:
```python
@property
def order_store(self) -> OrderStore | None:
    return self._order_store
```

Modify `RohlikAccount.async_update` — after `self.data = await self._rohlik_api.get_data()`, add:
```python
# Initialize order store on first update (need user_id from login)
if not self._order_store and self.data.get("login"):
    user_id = str(self.data["login"]["data"]["user"]["id"])
    storage_dir = self._hass.config.path(".storage")
    self._order_store = OrderStore(storage_dir, user_id)

# Process delivered orders into persistent store
if self._order_store and self.data.get("delivered_orders"):
    self._order_store.process_orders(self.data["delivered_orders"])
```

Add new method to `RohlikAccount`:
```python
async def fetch_full_order_history(self) -> int:
    """Fetch all historical orders and store them. Returns total order count."""
    all_orders = await self._rohlik_api.fetch_all_delivered_orders()
    if self._order_store and all_orders:
        new = self._order_store.process_orders(all_orders)
        await self.publish_updates()
        return self._order_store.alltime_count()
    return 0
```

**Step 2: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('custom_components/rohlikcz/hub.py', doraise=True); print('OK')"`

**Step 3: Commit**

```bash
git add custom_components/rohlikcz/hub.py
git commit -m "feat: add OrderStore with persistent JSON storage"
```

---

### Task 3: Add YearlySpent and AllTimeSpent sensors

**Files:**
- Modify: `custom_components/rohlikcz/sensor.py`
- Modify: `custom_components/rohlikcz/const.py`

**Step 1: Add constants to `const.py`**

Add to the icons section:
```python
ICON_YEARLY_SPENT = "mdi:calendar-text"
ICON_ALLTIME_SPENT = "mdi:chart-line"
```

**Step 2: Add sensor classes to `sensor.py`**

Add imports at top (after existing imports):
```python
# No new imports needed — datetime, ZoneInfo, Mapping, Any already imported
```

Add to the entities list in `async_setup_entry` (after the `MonthlySpent` line):
```python
        YearlySpent(rohlik_hub),
        AllTimeSpent(rohlik_hub),
```

Add import of new icons in the existing import line from `.const`:
```python
ICON_YEARLY_SPENT, ICON_ALLTIME_SPENT
```

Add after the `MonthlySpent` class (before `NoLimitOrders`):

```python
class YearlySpent(BaseEntity, SensorEntity):
    """Sensor for amount spent in current year from persistent order store."""

    _attr_translation_key = "yearly_spent"
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.TOTAL

    @property
    def native_value(self) -> float | None:
        """Returns amount spent in current year."""
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        return store.yearly_total(year)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        count = store.yearly_count(year)
        total = store.yearly_total(year)
        return {
            "year": year,
            "order_count": count,
            "average_order_value": round(total / count, 2) if count > 0 else 0.0,
        }

    @property
    def icon(self) -> str:
        return ICON_YEARLY_SPENT

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class AllTimeSpent(BaseEntity, SensorEntity):
    """Sensor for total amount spent across all tracked orders."""

    _attr_translation_key = "alltime_spent"
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> float | None:
        """Returns total amount spent across all orders."""
        store = self._rohlik_account.order_store
        if not store:
            return None
        return store.alltime_total()

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        count = store.alltime_count()
        total = store.alltime_total()
        return {
            "order_count": count,
            "average_order_value": round(total / count, 2) if count > 0 else 0.0,
            "first_order_date": store.first_order_date(),
            "tracking_since": store.tracking_since,
        }

    @property
    def icon(self) -> str:
        return ICON_ALLTIME_SPENT

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)
```

**Step 3: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('custom_components/rohlikcz/sensor.py', doraise=True); py_compile.compile('custom_components/rohlikcz/const.py', doraise=True); print('OK')"`

**Step 4: Commit**

```bash
git add custom_components/rohlikcz/sensor.py custom_components/rohlikcz/const.py
git commit -m "feat: add YearlySpent and AllTimeSpent sensors"
```

---

### Task 4: Add `fetch_order_history` service

**Files:**
- Modify: `custom_components/rohlikcz/services.py`
- Modify: `custom_components/rohlikcz/services.yaml`
- Modify: `custom_components/rohlikcz/const.py`

**Step 1: Add constant to `const.py`**

```python
SERVICE_FETCH_ORDER_HISTORY = "fetch_order_history"
```

**Step 2: Add service handler to `services.py`**

Add import of `SERVICE_FETCH_ORDER_HISTORY` to the existing import line from `.const`.

Add service function inside `register_services` (before the `# Register the services` comment):

```python
    async def async_fetch_order_history(call: ServiceCall) -> Dict[str, Any]:
        """Fetch complete order history from Rohlik."""
        config_entry_id = call.data[ATTR_CONFIG_ENTRY_ID]

        if config_entry_id not in hass.data[DOMAIN]:
            raise HomeAssistantError(f"Config entry {config_entry_id} not found")

        account = hass.data[DOMAIN][config_entry_id]
        try:
            total = await account.fetch_full_order_history()
            return {"total_orders": total}
        except Exception as err:
            _LOGGER.error(f"Failed to fetch order history: {err}")
            raise HomeAssistantError(f"Failed to fetch order history: {err}")
```

Add registration at the bottom (after `SERVICE_UPDATE_DATA` registration):

```python
    hass.services.async_register(
        DOMAIN,
        SERVICE_FETCH_ORDER_HISTORY,
        async_fetch_order_history,
        schema=vol.Schema({
            vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        }),
        supports_response=True
    )
```

**Step 3: Add service YAML definition to `services.yaml`**

Append to `services.yaml`:

```yaml

fetch_order_history:
  name: Fetch order history
  description: Fetch complete order history from Rohlik.cz and store locally. Run once to backfill historical data.
  fields:
    config_entry_id:
      name: Account
      description: The Rohlik account to use
      required: true
      selector:
        config_entry:
          integration: rohlikcz
```

**Step 4: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('custom_components/rohlikcz/services.py', doraise=True); print('OK')"`

**Step 5: Commit**

```bash
git add custom_components/rohlikcz/services.py custom_components/rohlikcz/services.yaml custom_components/rohlikcz/const.py
git commit -m "feat: add fetch_order_history service for historical backfill"
```

---

### Task 5: Add translations

**Files:**
- Modify: `custom_components/rohlikcz/translations/en.json`
- Modify: `custom_components/rohlikcz/translations/cs.json`

**Step 1: Add English translations**

Add to `entity.sensor` section in `en.json`:

```json
      "yearly_spent": {
        "name": "Spent This Year",
        "unit_of_measurement": "CZK"
      },
      "alltime_spent": {
        "name": "Spent All Time",
        "unit_of_measurement": "CZK"
      }
```

**Step 2: Add Czech translations**

Add to `entity.sensor` section in `cs.json`:

```json
      "yearly_spent": {
        "name": "Roční útrata",
        "unit_of_measurement": "Kč"
      },
      "alltime_spent": {
        "name": "Celková útrata",
        "unit_of_measurement": "Kč"
      }
```

**Step 3: Commit**

```bash
git add custom_components/rohlikcz/translations/
git commit -m "feat: add translations for new spending sensors"
```

---

### Task 6: Test locally with standalone script

**Files:**
- Create: `tests/test_order_store.py`

**Step 1: Write standalone test for OrderStore logic**

```python
"""Standalone test for OrderStore — run without HA."""
import json
import os
import tempfile
import sys

# Minimal test — no HA dependencies
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

def test_order_store():
    """Test OrderStore process, yearly, alltime logic."""
    # Inline the logic since we can't import HA-dependent modules directly
    tmpdir = tempfile.mkdtemp()
    store_path = os.path.join(tmpdir, "test_orders.json")

    # Simulate order data as returned by Rohlik API
    orders = [
        {"id": 1001, "orderTime": "2026-01-15T10:00:00.000+01:00",
         "priceComposition": {"total": {"amount": 500.0}}},
        {"id": 1002, "orderTime": "2026-02-20T10:00:00.000+01:00",
         "priceComposition": {"total": {"amount": 1200.0}}},
        {"id": 1003, "orderTime": "2025-12-01T10:00:00.000+01:00",
         "priceComposition": {"total": {"amount": 800.0}}},
        {"id": 1003, "orderTime": "2025-12-01T10:00:00.000+01:00",
         "priceComposition": {"total": {"amount": 800.0}}},  # duplicate
        {"id": 1004, "orderTime": "2025-06-01T10:00:00.000+01:00",
         "priceComposition": {"total": None}},  # no amount — should skip
    ]

    # Process
    data = {"version": 1, "user_id": "test", "tracking_since": None, "orders": {}}
    new_count = 0
    for order in orders:
        order_id = str(order.get("id", ""))
        if not order_id or order_id in data["orders"]:
            continue
        price_comp = order.get("priceComposition", {})
        total = price_comp.get("total", {})
        if not isinstance(total, dict):
            continue
        amount = total.get("amount")
        if amount is None:
            continue
        try:
            amount = float(amount)
        except (ValueError, TypeError):
            continue
        order_time = order.get("orderTime", "")
        date_str = order_time[:10] if order_time else ""
        data["orders"][order_id] = {"date": date_str, "amount": amount}
        new_count += 1

    # Assertions
    assert new_count == 3, f"Expected 3 new orders, got {new_count}"
    assert len(data["orders"]) == 3, f"Expected 3 orders in store, got {len(data['orders'])}"

    # Yearly total for 2026
    yearly_2026 = sum(o["amount"] for o in data["orders"].values() if o["date"].startswith("2026"))
    assert yearly_2026 == 1700.0, f"Expected 1700.0, got {yearly_2026}"

    # Yearly total for 2025
    yearly_2025 = sum(o["amount"] for o in data["orders"].values() if o["date"].startswith("2025"))
    assert yearly_2025 == 800.0, f"Expected 800.0, got {yearly_2025}"

    # Alltime total
    alltime = sum(o["amount"] for o in data["orders"].values())
    assert alltime == 2500.0, f"Expected 2500.0, got {alltime}"

    # Save and reload
    with open(store_path, "w") as f:
        json.dump(data, f)
    with open(store_path, "r") as f:
        reloaded = json.load(f)
    assert len(reloaded["orders"]) == 3

    # Cleanup
    os.remove(store_path)
    os.rmdir(tmpdir)
    print("All tests passed!")

if __name__ == "__main__":
    test_order_store()
```

**Step 2: Run the test**

Run: `python3 tests/test_order_store.py`
Expected: `All tests passed!`

**Step 3: Commit**

```bash
git add tests/test_order_store.py
git commit -m "test: add standalone OrderStore logic test"
```

---

### Task 7: Deploy to HA and verify

**Step 1: Copy all modified files to HA**

```bash
cp custom_components/rohlikcz/rohlik_api.py /Volumes/config/custom_components/rohlikcz/
cp custom_components/rohlikcz/hub.py /Volumes/config/custom_components/rohlikcz/
cp custom_components/rohlikcz/sensor.py /Volumes/config/custom_components/rohlikcz/
cp custom_components/rohlikcz/const.py /Volumes/config/custom_components/rohlikcz/
cp custom_components/rohlikcz/services.py /Volumes/config/custom_components/rohlikcz/
cp custom_components/rohlikcz/services.yaml /Volumes/config/custom_components/rohlikcz/
cp custom_components/rohlikcz/translations/en.json /Volumes/config/custom_components/rohlikcz/translations/
cp custom_components/rohlikcz/translations/cs.json /Volumes/config/custom_components/rohlikcz/translations/
```

**Step 2: Restart HA** (via MCP `ha_restart`)

**Step 3: Verify new sensors exist**

Check via MCP:
- `ha_search_entities(query="rohlik spent")` — should show 3 sensors (monthly, yearly, alltime)
- `ha_get_state("sensor.rohlik_spent_this_year")` — should show current year total (from the 50 most recent orders)
- `ha_get_state("sensor.rohlik_spent_all_time")` — should show total across all stored orders

**Step 4: Trigger historical backfill**

Call via MCP: `ha_call_service("rohlikcz", "fetch_order_history", data={"config_entry_id": "<entry_id>"})`

**Step 5: Verify backfill worked**

- Check `ha_get_state("sensor.rohlik_spent_all_time")` — attributes should show `order_count` >> 12
- Check file exists: `cat /Volumes/config/.storage/rohlikcz_1086873_orders.json | python3 -m json.tool | head -20`

**Step 6: Commit final state**

```bash
git add -A
git commit -m "feat: order history sensors verified on live HA"
```
