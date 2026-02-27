# Config Flow: Analytics Level Selection — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** After login, let users choose which spending analytics levels they want (L0–L3 categories, per-item), and only trigger enrichment if at least one is selected.

**Architecture:** Add a second config flow step (`async_step_analytics`) after successful login. Store user selections in `entry.options`. Also add an OptionsFlow so users can change selections later (triggers reload). Sensor creation and enrichment are gated by these options. Existing users who upgrade get all analytics disabled by default (no surprise API calls).

**Tech Stack:** Home Assistant ConfigFlow/OptionsFlow, `SelectSelector` with `multiple=True` + `mode=LIST` for checkboxes, voluptuous schemas.

---

## Background / Key Files

| File | Purpose |
|------|---------|
| `custom_components/rohlikcz/config_flow.py` | Login + new analytics step |
| `custom_components/rohlikcz/__init__.py` | Entry setup, options reload listener |
| `custom_components/rohlikcz/const.py` | New option constants |
| `custom_components/rohlikcz/sensor.py` | Conditional sensor creation |
| `custom_components/rohlikcz/hub.py` | Conditional enrichment |
| `custom_components/rohlikcz/translations/en.json` | English UI strings |
| `custom_components/rohlikcz/translations/cs.json` | Czech UI strings |
| `custom_components/rohlikcz/manifest.json` | Bump version |

**How HA config flow steps work:**
- Each step is a method `async_step_<name>` returning `async_show_form(step_id="<name>", ...)`.
- Store intermediate data between steps as `self.<attr>`.
- Only the final step calls `self.async_create_entry(title=..., data=..., options=...)`.
- `entry.data` = credentials (immutable). `entry.options` = user preferences (mutable via OptionsFlow).
- OptionsFlow entry point is always `async_step_init`.

**Analytics levels we offer:**

| Option value | Display (EN) | Display (CS) | Example |
|---|---|---|---|
| `categories_l0` | Top-level categories | Kategorie nejvyšší úrovně | Nápoje, Drogerie |
| `categories_l1` | Mid-level categories | Kategorie střední úrovně | Horké nápoje, Čisticí prostředky |
| `categories_l2` | Detailed categories | Podrobné kategorie | Káva, Univerzální čistič |
| `categories_l3` | Most specific categories | Nejpodrobnější kategorie | Zrnková káva, Ve spreji |
| `per_item` | Per-item spending | Útrata po položkách | Tchibo Barista, Savo sprej |

Each selected level creates 2 sensors: `*_this_year` and `*_all_time`. Per-item also creates 2 sensors.

**Enrichment rule:** If ANY option is selected → enrichment runs. If NONE selected → no enrichment, no order store, no extra API calls.

---

### Task 1: Add option constants to const.py

**Files:**
- Modify: `custom_components/rohlikcz/const.py`

**Step 1: Add the constants**

Add these at the end of `const.py`, after the existing service name constants:

```python
""" Analytics options """
CONF_ANALYTICS = "analytics"
ANALYTICS_OPTIONS = [
    "categories_l0",
    "categories_l1",
    "categories_l2",
    "categories_l3",
    "per_item",
]
DEFAULT_ANALYTICS = []  # Nothing enabled by default (opt-in)
```

**Step 2: Commit**

```bash
git add custom_components/rohlikcz/const.py
git commit -m "feat: add analytics option constants"
```

---

### Task 2: Add second config flow step (analytics selection)

**Files:**
- Modify: `custom_components/rohlikcz/config_flow.py`

**Step 1: Update config_flow.py**

Replace the entire file with:

```python
import logging
from typing import Any

from homeassistant.const import CONF_PASSWORD, CONF_EMAIL
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
import voluptuous as vol

from .const import DOMAIN, CONF_ANALYTICS, ANALYTICS_OPTIONS, DEFAULT_ANALYTICS
from .errors import InvalidCredentialsError
from .rohlik_api import RohlikCZAPI

_LOGGER = logging.getLogger(__name__)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Validate the user input allows us to connect."""
    api = RohlikCZAPI(data[CONF_EMAIL], data[CONF_PASSWORD])
    reply = await api.get_data()
    title: str = reply["login"]["data"]["user"]["name"]
    return title, data


ANALYTICS_SCHEMA = vol.Schema({
    vol.Optional(CONF_ANALYTICS, default=DEFAULT_ANALYTICS): SelectSelector(
        SelectSelectorConfig(
            options=ANALYTICS_OPTIONS,
            multiple=True,
            mode=SelectSelectorMode.LIST,
            translation_key=CONF_ANALYTICS,
        )
    ),
})


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL
    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._user_title: str | None = None
        self._user_data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:

        data_schema: dict[Any, Any] = {
            vol.Required(CONF_EMAIL, default="e-mail"): str,
            vol.Required(CONF_PASSWORD, default="password"): str,
        }

        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info, data = await validate_input(self.hass, user_input)
                self._user_title = info
                self._user_data = data
                return await self.async_step_analytics()

            except InvalidCredentialsError:
                errors["base"] = "invalid_auth"

            except Exception:
                _LOGGER.exception("Unknown exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(data_schema), errors=errors
        )

    async def async_step_analytics(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Second step: choose analytics levels."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._user_title,
                data=self._user_data,
                options={CONF_ANALYTICS: user_input.get(CONF_ANALYTICS, [])},
            )

        return self.async_show_form(
            step_id="analytics",
            data_schema=ANALYTICS_SCHEMA,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return RohlikOptionsFlowHandler()


class RohlikOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for existing entries (reconfigure analytics)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(CONF_ANALYTICS, DEFAULT_ANALYTICS)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_ANALYTICS, default=current): SelectSelector(
                    SelectSelectorConfig(
                        options=ANALYTICS_OPTIONS,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                        translation_key=CONF_ANALYTICS,
                    )
                ),
            }),
        )
```

**Key changes from original:**
- `VERSION` bumped from `0.1` to `1` (integer, required for options flow).
- `async_step_user` no longer calls `async_create_entry` directly — stores data and goes to step 2.
- `async_step_analytics` shows checkboxes and creates the entry with `options=`.
- `async_get_options_flow` registered so existing entries get a "Configure" button.
- `RohlikOptionsFlowHandler` lets users change analytics selections later.

**Step 2: Commit**

```bash
git add custom_components/rohlikcz/config_flow.py
git commit -m "feat: add analytics selection step to config flow"
```

---

### Task 3: Add translations for the analytics step

**Files:**
- Modify: `custom_components/rohlikcz/translations/en.json`
- Modify: `custom_components/rohlikcz/translations/cs.json`

**Step 1: Update en.json**

Add the `"analytics"` step inside `"config" > "step"`, and add a `"selector"` block at root level. The full `"config"` block becomes:

```json
{
  "config": {
    "step": {
      "user": {
        "title": "Rohlik.cz Account",
        "description": "Enter your Rohlik.cz login credentials.",
        "data": {
          "username": "Email",
          "password": "Password"
        },
        "data_description": {
          "username": "Your Rohlik.cz account email",
          "password": "Your Rohlik.cz account password"
        }
      },
      "analytics": {
        "title": "Spending Analytics",
        "description": "Choose which spending breakdowns to track. Each selected level creates sensors for this year and all time. Selecting any option will trigger a one-time download of your order history (may take several minutes).",
        "data": {
          "analytics": "Analytics levels"
        }
      }
    },
    "error": {
      "cannot_connect": "Failed to connect to Rohlik.cz",
      "invalid_auth": "Invalid email or password",
      "unknown": "Unexpected error"
    },
    "abort": {
      "already_configured": "Account is already configured"
    }
  },
  "options": {
    "step": {
      "init": {
        "title": "Spending Analytics",
        "description": "Choose which spending breakdowns to track. Changes take effect after reload.",
        "data": {
          "analytics": "Analytics levels"
        }
      }
    }
  },
  "selector": {
    "analytics": {
      "options": {
        "categories_l0": "Top-level categories (e.g. Drinks, Drugstore, Fruit & Vegetables)",
        "categories_l1": "Mid-level categories (e.g. Hot drinks, Cleaning products, Vegetables)",
        "categories_l2": "Detailed categories (e.g. Coffee, Universal cleaner, Tomatoes)",
        "categories_l3": "Most specific categories (e.g. Bean coffee, Spray cleaner)",
        "per_item": "Per-item spending (e.g. Tchibo Barista, Savo Spray)"
      }
    }
  },
  "entity": { ... }
}
```

**Note:** Keep the existing `"entity"` block unchanged. Only add/modify `"config.step.analytics"`, `"options"`, and `"selector"`.

**Step 2: Update cs.json**

Same structure, Czech text:

```json
{
  "config": {
    "step": {
      "user": { ... },
      "analytics": {
        "title": "Analýza nákupů",
        "description": "Vyberte, které úrovně útrat chcete sledovat. Pro každou vybranou úroveň se vytvoří senzory pro tento rok a celkově. Výběr jakékoli možnosti spustí jednorázové stažení historie objednávek (může trvat několik minut).",
        "data": {
          "analytics": "Úrovně analýzy"
        }
      }
    },
    ...
  },
  "options": {
    "step": {
      "init": {
        "title": "Analýza nákupů",
        "description": "Vyberte, které úrovně útrat chcete sledovat. Změny se projeví po reloadu.",
        "data": {
          "analytics": "Úrovně analýzy"
        }
      }
    }
  },
  "selector": {
    "analytics": {
      "options": {
        "categories_l0": "Nejvyšší kategorie (např. Nápoje, Drogerie, Ovoce a zelenina)",
        "categories_l1": "Střední kategorie (např. Horké nápoje, Čisticí prostředky, Zelenina)",
        "categories_l2": "Podrobné kategorie (např. Káva, Univerzální čistič, Rajčata)",
        "categories_l3": "Nejpodrobnější kategorie (např. Zrnková káva, Ve spreji)",
        "per_item": "Útrata po položkách (např. Tchibo Barista, Savo sprej)"
      }
    }
  },
  "entity": { ... }
}
```

**Step 3: Commit**

```bash
git add custom_components/rohlikcz/translations/en.json custom_components/rohlikcz/translations/cs.json
git commit -m "feat: add translations for analytics config step"
```

---

### Task 4: Add reload listener in __init__.py and pass options to hub

**Files:**
- Modify: `custom_components/rohlikcz/__init__.py`

**Step 1: Update __init__.py**

```python
"""Rohlík CZ custom component."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import DOMAIN, CONF_ANALYTICS, DEFAULT_ANALYTICS
from .hub import RohlikAccount
from .services import register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor", "binary_sensor", "todo", "calendar"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Rohlik integration from a config entry flow."""
    analytics = entry.options.get(CONF_ANALYTICS, DEFAULT_ANALYTICS)

    rohlik_hub = RohlikAccount(hass, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD], analytics=analytics)
    await rohlik_hub.async_update()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = rohlik_hub

    # Register services
    register_services(hass)

    _LOGGER.info("Setting up platforms: %s", PLATFORMS)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.info("Platforms setup complete")

    # Reload when options change (user reconfigures analytics)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
```

**Step 2: Commit**

```bash
git add custom_components/rohlikcz/__init__.py
git commit -m "feat: pass analytics options to hub and add reload listener"
```

---

### Task 5: Gate enrichment in hub.py behind analytics options

**Files:**
- Modify: `custom_components/rohlikcz/hub.py`

**Step 1: Update RohlikAccount.__init__ to accept analytics**

Change the `__init__` signature:

```python
def __init__(self, hass: HomeAssistant, username: str, password: str, analytics: list[str] | None = None) -> None:
    """Initialize account info."""
    super().__init__()
    self._hass = hass
    self._username: str = username
    self._password: str = password
    self._rohlik_api = RohlikCZAPI(self._username, self._password)
    self.data: dict = {}
    self._callbacks: set[Callable[[], None]] = set()
    self._order_store: OrderStore | None = None
    self._analytics: list[str] = analytics or []
```

**Step 2: Add a property to check if analytics is enabled**

```python
@property
def analytics_enabled(self) -> bool:
    """Whether any analytics level is selected."""
    return len(self._analytics) > 0

@property
def analytics(self) -> list[str]:
    """Selected analytics levels."""
    return self._analytics
```

**Step 3: Gate the order store init and enrichment in async_update**

In the `async_update` method, wrap the order store block:

```python
# Initialize order store on first update (only if analytics enabled)
if self._analytics and not self._order_store and self.data.get("login"):
    user_id = str(self.data["login"]["data"]["user"]["id"])
    storage_dir = self._hass.config.path(".storage")
    self._order_store = await OrderStore.async_create(storage_dir, user_id, self._hass)

# Process delivered orders into persistent store and auto-enrich new ones
if self._analytics and self._order_store and self.data.get("delivered_orders"):
    ...  # existing enrichment code unchanged
```

Just add `self._analytics and` as a guard to both `if` blocks. Rest stays the same.

**Step 4: Commit**

```bash
git add custom_components/rohlikcz/hub.py
git commit -m "feat: gate order store and enrichment behind analytics options"
```

---

### Task 6: Create sensors conditionally based on options

**Files:**
- Modify: `custom_components/rohlikcz/sensor.py`

**Step 1: Add new sensor classes for L0, L2, L3, and per-item**

Add these classes after the existing `CategorySpendingAllTime` class. They follow the exact same pattern but with different `level` values and translation keys.

```python
class CategorySpendingL0Yearly(BaseEntity, SensorEntity):
    """L0 category spending this year."""
    _attr_translation_key = "categories_l0_this_year"
    _attr_should_poll = False

    @property
    def native_value(self) -> int | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        return len(store.category_totals(year=year, level=0))

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        categories = store.category_totals(year=year, level=0)
        return {"year": year, "categories": categories} if categories else {"year": year}

    @property
    def icon(self) -> str:
        return ICON_CATEGORY_SPENDING

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class CategorySpendingL0AllTime(BaseEntity, SensorEntity):
    """L0 category spending all time."""
    _attr_translation_key = "categories_l0_all_time"
    _attr_should_poll = False

    @property
    def native_value(self) -> int | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        return len(store.category_totals(level=0))

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        categories = store.category_totals(level=0)
        return {"categories": categories, "products_in_cache": store.cached_product_count} if categories else None

    @property
    def icon(self) -> str:
        return ICON_CATEGORY_SPENDING

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)
```

Create the same pattern for L2 (`level=2`, translation keys `categories_l2_this_year`/`categories_l2_all_time`) and L3 (`level=3`, translation keys `categories_l3_this_year`/`categories_l3_all_time`).

**Step 2: Add per-item sensor classes**

These need a new `item_totals()` method on `OrderStore` (see Task 7). The sensor classes:

```python
class ItemSpendingYearly(BaseEntity, SensorEntity):
    """Per-item spending this year."""
    _attr_translation_key = "items_this_year"
    _attr_should_poll = False

    @property
    def native_value(self) -> int | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        return len(store.item_totals(year=year))

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        items = store.item_totals(year=year)
        return {"year": year, "items": items} if items else {"year": year}

    @property
    def icon(self) -> str:
        return ICON_CATEGORY_SPENDING

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class ItemSpendingAllTime(BaseEntity, SensorEntity):
    """Per-item spending all time."""
    _attr_translation_key = "items_all_time"
    _attr_should_poll = False

    @property
    def native_value(self) -> int | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        return len(store.item_totals())

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        items = store.item_totals()
        return {"items": items, "products_in_cache": store.cached_product_count} if items else None

    @property
    def icon(self) -> str:
        return ICON_CATEGORY_SPENDING

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)
```

**Step 3: Update async_setup_entry to conditionally add sensors**

```python
async def async_setup_entry(hass, config_entry, async_add_entities):
    rohlik_hub = hass.data[DOMAIN][config_entry.entry_id]
    analytics = rohlik_hub.analytics

    entities = [
        # ... all existing base sensors (FirstDelivery through MonthlySpent) ...
    ]

    # Spending sensors that need the order store (always created if analytics enabled,
    # since they depend on the same order data)
    if analytics:
        entities.append(YearlySpent(rohlik_hub))
        entities.append(AllTimeSpent(rohlik_hub))

    # Category sensors per selected level
    if "categories_l0" in analytics:
        entities.append(CategorySpendingL0Yearly(rohlik_hub))
        entities.append(CategorySpendingL0AllTime(rohlik_hub))
    if "categories_l1" in analytics:
        entities.append(CategorySpendingYearly(rohlik_hub))
        entities.append(CategorySpendingAllTime(rohlik_hub))
    if "categories_l2" in analytics:
        entities.append(CategorySpendingL2Yearly(rohlik_hub))
        entities.append(CategorySpendingL2AllTime(rohlik_hub))
    if "categories_l3" in analytics:
        entities.append(CategorySpendingL3Yearly(rohlik_hub))
        entities.append(CategorySpendingL3AllTime(rohlik_hub))
    if "per_item" in analytics:
        entities.append(ItemSpendingYearly(rohlik_hub))
        entities.append(ItemSpendingAllTime(rohlik_hub))

    # ... existing conditional sensors (express slot, premium) ...
```

**Important:** `YearlySpent` and `AllTimeSpent` are moved inside the `if analytics:` block since they also depend on the order store. `MonthlySpent` stays outside because it uses `delivered_orders` from the API directly (no store needed).

**Step 4: Commit**

```bash
git add custom_components/rohlikcz/sensor.py
git commit -m "feat: conditionally create analytics sensors based on config options"
```

---

### Task 7: Add item_totals() to OrderStore

**Files:**
- Modify: `custom_components/rohlikcz/hub.py`

**Step 1: Add the method**

Add after `category_totals()`:

```python
def item_totals(self, year: str | None = None) -> list[dict]:
    """Aggregate spending by individual product. Optionally filter by year.

    Returns sorted list: [{"name": "Tchibo Barista", "id": 1328327, "spent": 500.0, "units": 10, "avg_unit_price": 50.0}]
    """
    from collections import defaultdict
    items = defaultdict(lambda: {"name": "", "spent": 0.0, "units": 0})

    for order in self._data["orders"].values():
        if year and not order["date"].startswith(year):
            continue
        for item in order.get("items", []):
            pid = item.get("id")
            if not pid:
                continue
            items[pid]["name"] = item.get("name", "Unknown")
            items[pid]["spent"] += item.get("price", 0)
            items[pid]["units"] += item.get("quantity", 1)

    result = []
    for pid, vals in items.items():
        avg = round(vals["spent"] / vals["units"], 2) if vals["units"] > 0 else 0.0
        result.append({
            "name": vals["name"],
            "id": pid,
            "spent": round(vals["spent"], 2),
            "units": vals["units"],
            "avg_unit_price": avg,
        })
    result.sort(key=lambda x: x["spent"], reverse=True)
    return result
```

**Step 2: Commit**

```bash
git add custom_components/rohlikcz/hub.py
git commit -m "feat: add item_totals() aggregation to OrderStore"
```

---

### Task 8: Add translations for new sensor entities

**Files:**
- Modify: `custom_components/rohlikcz/translations/en.json`
- Modify: `custom_components/rohlikcz/translations/cs.json`

**Step 1: Add sensor translation keys**

In `en.json`, inside `"entity" > "sensor"`, add:

```json
"categories_l0_this_year": { "name": "Top Categories This Year" },
"categories_l0_all_time": { "name": "Top Categories All Time" },
"categories_l2_this_year": { "name": "Detailed Categories This Year" },
"categories_l2_all_time": { "name": "Detailed Categories All Time" },
"categories_l3_this_year": { "name": "Specific Categories This Year" },
"categories_l3_all_time": { "name": "Specific Categories All Time" },
"items_this_year": { "name": "Items This Year" },
"items_all_time": { "name": "Items All Time" }
```

In `cs.json`, same keys:

```json
"categories_l0_this_year": { "name": "Hlavní kategorie tento rok" },
"categories_l0_all_time": { "name": "Hlavní kategorie celkem" },
"categories_l2_this_year": { "name": "Podrobné kategorie tento rok" },
"categories_l2_all_time": { "name": "Podrobné kategorie celkem" },
"categories_l3_this_year": { "name": "Specifické kategorie tento rok" },
"categories_l3_all_time": { "name": "Specifické kategorie celkem" },
"items_this_year": { "name": "Položky tento rok" },
"items_all_time": { "name": "Položky celkem" }
```

**Step 2: Commit**

```bash
git add custom_components/rohlikcz/translations/en.json custom_components/rohlikcz/translations/cs.json
git commit -m "feat: add translations for all analytics sensor levels"
```

---

### Task 9: Bump version and handle migration for existing entries

**Files:**
- Modify: `custom_components/rohlikcz/manifest.json`
- Modify: `custom_components/rohlikcz/config_flow.py`

**Step 1: Bump version in manifest.json**

Change `"version"` from `"0.3.1"` to `"0.4.0"`.

**Step 2: Add migration handler for existing entries**

In `__init__.py`, add migration so existing installs don't break. Before the `async_setup_entry` function:

```python
async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries to new format."""
    _LOGGER.debug("Migrating from version %s", entry.version)

    if entry.version < 1:
        # Pre-analytics entries: set empty analytics (opt-in)
        new_options = {**entry.options, CONF_ANALYTICS: DEFAULT_ANALYTICS}
        hass.config_entries.async_update_entry(entry, options=new_options, version=1)
        _LOGGER.info("Migrated config entry to version 1 (analytics disabled by default)")

    return True
```

**Step 3: Commit**

```bash
git add custom_components/rohlikcz/manifest.json custom_components/rohlikcz/__init__.py
git commit -m "feat: bump version to 0.4.0 and add config entry migration"
```

---

### Task 10: Manual testing

**No code changes — testing only.**

#### Option A: Uninstall and reinstall on existing HA

1. **Go to HA UI** → Settings → Devices & Services → Rohlík.cz
2. Click the three dots → **Delete**
3. Wait for full unload
4. **Deploy updated files:**
   ```bash
   cp -r custom_components/rohlikcz/* /Volumes/config/custom_components/rohlikcz/
   ```
5. **Restart HA** (Settings → System → Restart)
6. After restart, go to Settings → Devices & Services → **Add Integration** → search "Rohlik"
7. **Step 1 (Login):** Enter email/password → Submit
8. **Verify:** After successful login, you should see the **second step** with checkboxes
9. **Step 2 (Analytics):** Check "Mid-level categories" (L1) → Submit
10. **Verify sensors:** Go to Developer Tools → States → filter "rohlik"
    - `sensor.rohlik_categories_this_year` should appear
    - `sensor.rohlik_categories_all_time` should appear
    - Other unchecked levels should NOT appear
11. **Test OptionsFlow:** Go to Settings → Devices & Services → Rohlík.cz → **Configure**
    - You should see the analytics checkboxes with L1 pre-selected
    - Check "Per-item spending" too → Submit
    - Integration reloads → new `sensor.rohlik_items_this_year` and `items_all_time` appear
12. **Test unchecking all:** Configure again → uncheck everything → Submit
    - All analytics sensors should disappear
    - `YearlySpent` and `AllTimeSpent` should also disappear
    - No enrichment runs (check logs)

#### Option B: Fresh Docker instance (recommended for clean test)

1. **Spin up a fresh HA container:**
   ```bash
   docker run -d --name ha-test \
     -p 8124:8123 \
     -v /tmp/ha-test-config:/config \
     homeassistant/home-assistant:latest
   ```
2. **Wait for startup** (~2 min), open `http://localhost:8124`
3. **Complete onboarding** (create account, skip integrations)
4. **Copy the integration:**
   ```bash
   mkdir -p /tmp/ha-test-config/custom_components/rohlikcz
   cp -r custom_components/rohlikcz/* /tmp/ha-test-config/custom_components/rohlikcz/
   ```
5. **Restart the container:**
   ```bash
   docker restart ha-test
   ```
6. **Add the integration:** Settings → Add Integration → Rohlik
7. **Walk through both steps** as described in Option A steps 7-12
8. **Verify enrichment notification:** After selecting any analytics level, check notifications (bell icon) for "Rohlik: Enrichment in progress"
9. **Clean up:**
   ```bash
   docker stop ha-test && docker rm ha-test
   rm -rf /tmp/ha-test-config
   ```

#### Test matrix (check each):

| Test | Expected |
|------|----------|
| Login with valid credentials | Goes to analytics step (not direct entry creation) |
| Login with invalid credentials | Shows error, stays on login step |
| Analytics step: select nothing → Submit | Entry created, no analytics sensors, no enrichment |
| Analytics step: select L1 only → Submit | Only L1 sensors + YearlySpent + AllTimeSpent created |
| Analytics step: select all → Submit | All 12 analytics sensors created |
| Options flow: add L0 to existing | Integration reloads, L0 sensors appear |
| Options flow: remove all analytics | All analytics sensors removed, enrichment stops |
| Existing entry migration (upgrade from old version) | Entry migrated, analytics empty, no sensors break |

---

### Task 11: Deploy to production HA and final commit

**Step 1: Deploy all files**

```bash
cp -r custom_components/rohlikcz/* /Volumes/config/custom_components/rohlikcz/
```

**Step 2: Restart HA**

The existing entry will be migrated (analytics = empty). Go to Configure to enable desired analytics levels.

**Step 3: Final commit and squash if needed**

```bash
git add -A
git commit -m "feat: add config flow step for analytics level selection with OptionsFlow"
```
