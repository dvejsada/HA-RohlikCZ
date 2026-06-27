from __future__ import annotations
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Optional, Dict
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import DOMAIN
from .errors import InvalidCredentialsError, APIRequestFailedError, RohlikczError
from .rohlik_api import RohlikCZAPI

#: How often the integration refreshes data from the Rohlik API.
UPDATE_INTERVAL = timedelta(seconds=600)

_LOGGER = logging.getLogger(__name__)

_NOTIFICATIONS = {
    "en": {
        "title_progress": "Rohlik: Enrichment in progress",
        "title_complete": "Rohlik: Enrichment complete",
        "phase1": "Phase 1/2: Fetching item details for {count} orders...",
        "phase2": "Phase 2/2: Categorizing {count} products...",
        "phase2_progress": "Phase 2/2: Categorizing products: {done}/{total} ({pct}%)",
        "complete": (
            "Enrichment complete!\n"
            "Orders enriched: {orders_enriched}\n"
            "Products categorized: {products_categorized}\n"
            "Total orders: {total_orders} (enriched: {enriched_orders})\n"
            "Products in cache: {products_in_cache}"
        ),
    },
    "cs": {
        "title_progress": "Rohlik: Probíhá obohacování dat",
        "title_complete": "Rohlik: Obohacování dokončeno",
        "phase1": "Fáze 1/2: Stahování položek pro {count} objednávek...",
        "phase2": "Fáze 2/2: Kategorizace {count} produktů...",
        "phase2_progress": "Fáze 2/2: Kategorizace produktů: {done}/{total} ({pct} %)",
        "complete": (
            "Obohacování dokončeno!\n"
            "Obohacené objednávky: {orders_enriched}\n"
            "Kategorizované produkty: {products_categorized}\n"
            "Celkem objednávek: {total_orders} (obohacených: {enriched_orders})\n"
            "Produktů v mezipaměti: {products_in_cache}"
        ),
    },
}


#: Storage schema version for the order store.
STORE_VERSION = 3


class OrderStore:
    """Persistent storage for order history backed by Home Assistant's Store helper."""

    def __init__(self, hass: HomeAssistant, user_id: str):
        self._hass = hass
        self._user_id = user_id
        self._key = f"rohlikcz_{user_id}_orders"
        self._store: Store[dict] = Store(hass, STORE_VERSION, self._key)
        # Path of the pre-Store implementation, imported once if present.
        self._legacy_path = hass.config.path(".storage", f"{self._key}.json")
        self._data = self._default_data(user_id)

    @staticmethod
    def _default_data(user_id: str) -> dict:
        return {"version": STORE_VERSION, "user_id": user_id, "tracking_since": None,
                "backfill_complete": False, "orders": {}, "product_categories": {}}

    @classmethod
    async def async_create(cls, hass: HomeAssistant, user_id: str) -> "OrderStore":
        """Create and load an OrderStore asynchronously."""
        store = cls(hass, user_id)
        await store._async_load()
        return store

    async def _async_load(self) -> None:
        """Load from Store, importing the legacy JSON file on first run."""
        data = await self._store.async_load()
        if data is not None:
            self._data = data
            return

        # No Store data yet: import the legacy file if one exists.
        legacy = await self._hass.async_add_executor_job(self._load_legacy_sync)
        if legacy is not None:
            self._data = self._migrate_legacy(legacy, self._user_id)
            await self._store.async_save(self._data)
            await self._hass.async_add_executor_job(self._remove_legacy_sync)
            _LOGGER.info("Imported legacy order store for user %s into HA storage", self._user_id)

    def _load_legacy_sync(self) -> dict | None:
        """Read the legacy JSON file (blocking, run in executor)."""
        if not os.path.exists(self._legacy_path):
            return None
        try:
            with open(self._legacy_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as err:
            _LOGGER.error("Failed to load legacy order store: %s", err)
            return None

    def _remove_legacy_sync(self) -> None:
        """Delete the legacy JSON file after a successful import."""
        try:
            os.remove(self._legacy_path)
        except OSError as err:
            _LOGGER.warning("Could not remove legacy order store file %s: %s", self._legacy_path, err)

    @staticmethod
    def _migrate_legacy(data: dict, user_id: str = "") -> dict:
        """Bring imported legacy data up to the current schema (v1/v2 -> v3)."""
        if not isinstance(data, dict):
            return OrderStore._default_data(user_id)
        data = dict(data)  # avoid mutating the caller's dict
        data.setdefault("orders", {})
        data.setdefault("product_categories", {})
        if "backfill_complete" not in data:
            # A store that already had a tracking_since had completed backfill.
            data["backfill_complete"] = data.get("tracking_since") is not None
        data["version"] = STORE_VERSION
        return data

    async def async_save(self) -> None:
        """Persist the order store asynchronously."""
        await self._store.async_save(self._data)

    def process_orders(self, orders: list) -> int:
        """Process a list of order dicts from the API. Returns count of new orders added.

        Note: Caller must call await async_save() after this if new_count > 0.
        """
        if not orders:
            return 0

        new_count = 0
        for order in orders:
            order_id = str(order.get("id", ""))
            if not order_id or order_id in self._data["orders"]:
                continue

            price_comp = order.get("priceComposition", {})
            if not isinstance(price_comp, dict):
                continue

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

            self._data["orders"][order_id] = {"date": date_str, "amount": amount}
            new_count += 1

        if new_count > 0:
            if not self._data["tracking_since"]:
                self._data["tracking_since"] = datetime.now(ZoneInfo("Europe/Prague")).isoformat()
            _LOGGER.info(f"Added {new_count} new orders to store. Total: {len(self._data['orders'])}")

        return new_count

    @property
    def orders(self) -> dict:
        return self._data["orders"]

    @property
    def tracking_since(self) -> str | None:
        return self._data.get("tracking_since")

    @property
    def backfill_complete(self) -> bool:
        """Whether the initial full order history download has finished."""
        return self._data.get("backfill_complete", False)

    def mark_backfill_complete(self) -> None:
        """Mark the initial backfill as done (caller must async_save)."""
        self._data["backfill_complete"] = True

    def yearly_total(self, year: str) -> float:
        """Sum of order amounts for a given year (e.g. '2026')."""
        return sum(
            o["amount"] for o in self._data["orders"].values()
            if o["date"].startswith(year)
        )

    def yearly_count(self, year: str) -> int:
        """Count of orders for a given year."""
        return sum(1 for o in self._data["orders"].values() if o["date"].startswith(year))

    def yearly_enriched_count(self, year: str) -> int:
        """Count of enriched orders (with item details) for a given year."""
        return sum(1 for o in self._data["orders"].values() if o["date"].startswith(year) and "items" in o)

    def yearly_breakdown(self) -> dict[str, dict]:
        """Per-year totals and counts for every tracked year.

        Returns a mapping like {"2025": {"total": 1234.5, "order_count": 12}, ...}
        sorted by year descending (most recent first).
        """
        breakdown: dict[str, dict] = {}
        for order in self._data["orders"].values():
            date = order.get("date") or ""
            year = date[:4]
            if len(year) != 4:
                continue
            entry = breakdown.setdefault(year, {"total": 0.0, "order_count": 0})
            entry["total"] += order["amount"]
            entry["order_count"] += 1

        for entry in breakdown.values():
            entry["total"] = round(entry["total"], 2)

        return {year: breakdown[year] for year in sorted(breakdown, reverse=True)}

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

    def add_items_to_order(self, order_id: str, items: list) -> bool:
        """Add item details to an existing order. Returns True if added."""
        if order_id not in self._data["orders"]:
            return False
        if "items" in self._data["orders"][order_id]:
            return False  # Already has items
        self._data["orders"][order_id]["items"] = items
        return True

    def update_product_categories(self, categories_map: dict[int, list]) -> int:
        """Update the product->category cache. Returns count of new entries.

        Note: Caller must call await async_save() after this if new_count > 0.
        """
        new_count = 0
        for pid, cats in categories_map.items():
            pid_str = str(pid)
            if pid_str not in self._data["product_categories"]:
                # Store simplified: {pid: {l0: name, l1: name, l2: name, l3: name}}
                cat_dict = {}
                for cat in cats:
                    level = cat.get("level")
                    if level is not None:
                        cat_dict[f"l{level}"] = cat.get("name", "Unknown")
                self._data["product_categories"][pid_str] = cat_dict
                new_count += 1
        return new_count

    def get_product_category(self, product_id: int, level: int = 1) -> str:
        """Get category name for a product at a given level. Returns 'Uncategorized' if not found."""
        pid_str = str(product_id)
        cat_data = self._data.get("product_categories", {}).get(pid_str, {})
        return cat_data.get(f"l{level}", "Uncategorized")

    def _is_discontinued(self, product_id: int) -> bool:
        """Check if a product is discontinued (has sentinel 'Discontinued' category)."""
        pid_str = str(product_id)
        cat_data = self._data.get("product_categories", {}).get(pid_str, {})
        return cat_data.get("l1") == "Discontinued"

    @property
    def unenriched_order_ids(self) -> list[str]:
        """Order IDs that don't have item details yet."""
        return [oid for oid, o in self._data["orders"].items() if "items" not in o]

    @property
    def enriched_count(self) -> int:
        """Count of orders with item details."""
        return sum(1 for o in self._data["orders"].values() if "items" in o)

    @property
    def cached_product_count(self) -> int:
        """Count of products with cached categories."""
        return len(self._data.get("product_categories", {}))

    def uncategorized_product_ids(self) -> list[int]:
        """Product IDs found in enriched orders but not yet in category cache."""
        known = set(self._data.get("product_categories", {}).keys())
        found = set()
        for order in self._data["orders"].values():
            for item in order.get("items", []):
                pid = item.get("id")
                if pid is not None:
                    found.add(str(pid))
        return [int(pid) for pid in found - known]

    def category_totals(self, year: str | None = None, level: int = 1, hide_discontinued: bool = False) -> list[dict]:
        """Aggregate spending by category at given level. Optionally filter by year/month prefix.

        Returns sorted list: [{"name": "Dairy", "spent": 5000.0, "units": 200, "avg_unit_price": 25.0}]
        """
        from collections import defaultdict
        cats = defaultdict(lambda: {"spent": 0.0, "units": 0})

        for order in self._data["orders"].values():
            if year and not order["date"].startswith(year):
                continue
            for item in order.get("items", []):
                pid = item.get("id")
                if hide_discontinued and pid and self._is_discontinued(pid):
                    continue
                cat_name = self.get_product_category(pid, level) if pid else "Uncategorized"
                cats[cat_name]["spent"] += item.get("price", 0)
                cats[cat_name]["units"] += item.get("quantity", 1)

        if hide_discontinued:
            cats.pop("Discontinued", None)

        result = []
        for name, vals in cats.items():
            avg = round(vals["spent"] / vals["units"], 2) if vals["units"] > 0 else 0.0
            result.append({
                "name": name,
                "spent": round(vals["spent"], 2),
                "units": vals["units"],
                "avg_unit_price": avg,
            })
        result.sort(key=lambda x: x["spent"], reverse=True)
        return result

    def item_totals(self, year: str | None = None, hide_discontinued: bool = False) -> list[dict]:
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
                if hide_discontinued and self._is_discontinued(pid):
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


class RohlikAccount(DataUpdateCoordinator[dict]):
    """RohlikCZ account modelled as a Home Assistant data update coordinator."""

    def __init__(self, hass: HomeAssistant, username: str, password: str, analytics: list[str] | None = None, top_n: int = 10, hide_discontinued: bool = True, entry: ConfigEntry | None = None) -> None:
        """Initialize account info."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        self._username: str = username
        self._password: str = password
        self._rohlik_api = RohlikCZAPI(self._username, self._password)
        self._order_store: OrderStore | None = None
        self._last_refresh: datetime | None = None
        # _store_lock guards brief in-memory store mutations (contended by the
        # refresh cycle). _enrich_lock serializes whole enrichment runs and may
        # be held across network I/O without blocking the refresh.
        self._store_lock = asyncio.Lock()
        self._enrich_lock = asyncio.Lock()
        self._analytics: list[str] = analytics or []
        self._top_n: int = top_n
        self._hide_discontinued: bool = hide_discontinued

    @property
    def analytics_enabled(self) -> bool:
        """Whether any analytics level is selected."""
        return len(self._analytics) > 0

    @property
    def analytics(self) -> list[str]:
        """Selected analytics levels."""
        return self._analytics

    @property
    def top_n(self) -> int:
        """Number of top items to include in sensor attributes."""
        return self._top_n

    @property
    def hide_discontinued(self) -> bool:
        """Whether to exclude discontinued products from top N."""
        return self._hide_discontinued

    @property
    def has_address(self) -> bool:
        return bool((self.data or {}).get("next_delivery_slot"))

    @property
    def device_info(self) -> DeviceInfo:
        """ Provides a device info. """
        return {"identifiers": {(DOMAIN, self.data["login"]["data"]["user"]["id"])}, "name": self.data["login"]["data"]["user"]["name"], "manufacturer": "Rohlík.cz"}

    @property
    def account_name(self) -> str:
        """Provides display name for the account holder.

        Note: ``name`` is reserved by DataUpdateCoordinator, so the account's
        display name is exposed separately.
        """
        return self.data["login"]["data"]["user"]["name"]

    @property
    def unique_id(self) -> str:
        """Return the unique ID for this account."""
        return self.data["login"]["data"]["user"]["id"]

    @property
    def is_ordered(self) -> bool:
        return len(self.data.get('next_order', [])) > 0

    @property
    def order_store(self) -> OrderStore | None:
        return self._order_store

    @property
    def last_refresh(self) -> datetime | None:
        """Timestamp of the last data fetch from the API."""
        return self._last_refresh

    async def _async_update_data(self) -> dict:
        """Fetch data from the Rohlik API (called by the coordinator)."""
        try:
            data = await self._rohlik_api.get_data()
        except InvalidCredentialsError as err:
            # Credentials are no longer valid - trigger the reauth flow.
            raise ConfigEntryAuthFailed(str(err)) from err
        except (APIRequestFailedError, RohlikczError) as err:
            raise UpdateFailed(str(err)) from err

        self._last_refresh = datetime.now(ZoneInfo("Europe/Prague"))

        # Initialize order store on first update (only if analytics enabled)
        if self._analytics and not self._order_store and data.get("login"):
            user_id = str(data["login"]["data"]["user"]["id"])
            self._order_store = await OrderStore.async_create(self.hass, user_id)

        # Process delivered orders into persistent store and auto-enrich new ones
        if self._analytics and self._order_store and data.get("delivered_orders"):
            async with self._store_lock:
                new = self._order_store.process_orders(data["delivered_orders"])
                if new > 0:
                    await self._order_store.async_save()
            if new > 0:
                # Schedule enrichment in background (don't block update cycle / setup)
                self.config_entry.async_create_background_task(
                    self.hass, self._auto_enrich_new_orders(new), "rohlik_auto_enrich"
                )

        return data

    async def async_update(self) -> None:
        """Force an immediate data refresh (used by services and after cart changes)."""
        await self.async_refresh()

    async def _auto_enrich_new_orders(self, new_count: int) -> None:
        """Auto-enrich recently added orders in the background.

        The store lock is held only around in-memory reads/writes, never during
        network I/O, so a slow enrichment doesn't block the regular refresh
        cycle (which also takes the lock).
        """
        try:
            # Serialize against manual backfill/enrichment runs.
            async with self._enrich_lock:
                # Snapshot which orders still need enrichment.
                async with self._store_lock:
                    unenriched = list(self._order_store.unenriched_order_ids)
                    recent_unenriched = unenriched[-new_count:] if len(unenriched) >= new_count else unenriched
                if not recent_unenriched:
                    return

                # Fetch item details (network I/O, store lock released).
                items_map = await self._rohlik_api.enrich_orders_with_items(recent_unenriched)

                # Apply item results and find products needing categories.
                async with self._store_lock:
                    for order_id, items in items_map.items():
                        self._order_store.add_items_to_order(order_id, items)
                    uncategorized = self._order_store.uncategorized_product_ids()

                # Fetch categories (network I/O, store lock released).
                cat_map = {}
                if uncategorized:
                    cat_map = await self._rohlik_api.fetch_product_categories_batch(uncategorized)

                # Persist all results.
                enriched = False
                async with self._store_lock:
                    if cat_map:
                        self._order_store.update_product_categories(cat_map)
                    if items_map:
                        await self._order_store.async_save()
                        enriched = True

            if enriched:
                _LOGGER.info(f"Auto-enriched {len(items_map)} new orders")
                self.async_update_listeners()
        except Exception as err:
            _LOGGER.warning(f"Auto-enrichment of new orders failed: {err}")

    async def fetch_full_order_history(self, hass=None) -> dict:
        """Fetch all historical orders, enrich with items, and categorize products.

        Returns dict with stats.
        """
        async with self._enrich_lock:
            # Step 1: Fetch order list (network I/O, store lock released)
            all_orders = await self._rohlik_api.fetch_all_delivered_orders()
            new_orders = 0
            if self._order_store and all_orders:
                async with self._store_lock:
                    new_orders = self._order_store.process_orders(all_orders)
                    if new_orders > 0:
                        await self._order_store.async_save()

            # Step 2: Enrich with items and categories (manages its own lock)
            if self._order_store:
                enrich_stats = await self._enrich_order_details(hass=hass)
                enrich_stats["new_orders"] = new_orders
                # Mark backfill as done so we don't re-download on next restart
                if not self._order_store.backfill_complete:
                    async with self._store_lock:
                        self._order_store.mark_backfill_complete()
                        await self._order_store.async_save()
                return enrich_stats

            return {"total_orders": 0, "new_orders": 0}

    def _t(self, key: str) -> str:
        """Get localized notification string based on HA language."""
        lang = self.hass.config.language or "en"
        return _NOTIFICATIONS.get(lang, _NOTIFICATIONS["en"]).get(key, _NOTIFICATIONS["en"][key])

    async def _notify(self, hass, message: str, title: str, notification_id: str = "rohlik_enrichment") -> None:
        """Create a persistent notification via service call."""
        if not hass:
            return
        await hass.services.async_call(
            "persistent_notification", "create",
            {"message": message, "title": title, "notification_id": notification_id},
        )

    async def enrich_order_details(self, hass=None) -> dict:
        """Fetch item details and categories for all unenriched orders.

        Public entry point — serializes enrichment runs via _enrich_lock.
        """
        async with self._enrich_lock:
            return await self._enrich_order_details(hass=hass)

    async def _enrich_order_details(self, hass=None) -> dict:
        """Internal enrichment logic — caller must hold _enrich_lock.

        Two-phase enrichment:
        1. Fetch items for orders missing them
        2. Fetch categories for products not yet in cache

        The store lock is taken only around in-memory mutations, never during
        the network calls, so the regular refresh cycle isn't blocked.

        Returns stats dict.
        """
        if not self._order_store:
            return {"error": "Order store not initialized"}

        stats = {"orders_enriched": 0, "products_categorized": 0, "errors": 0}

        # Phase 1: Fetch items for unenriched orders
        async with self._store_lock:
            unenriched = list(self._order_store.unenriched_order_ids)
        if unenriched:
            await self._notify(hass,
                self._t("phase1").format(count=len(unenriched)),
                self._t("title_progress"))
            _LOGGER.info(f"Enriching {len(unenriched)} orders with item details...")
            items_map = await self._rohlik_api.enrich_orders_with_items(unenriched)
            async with self._store_lock:
                for order_id, items in items_map.items():
                    if self._order_store.add_items_to_order(order_id, items):
                        stats["orders_enriched"] += 1
                if stats["orders_enriched"] > 0:
                    await self._order_store.async_save()
            if stats["orders_enriched"] > 0:
                _LOGGER.info(f"Added items to {stats['orders_enriched']} orders")

        # Phase 2: Fetch categories for uncategorized products
        async with self._store_lock:
            uncategorized = self._order_store.uncategorized_product_ids()
        if uncategorized:
            total_products = len(uncategorized)
            await self._notify(hass,
                self._t("phase2").format(count=total_products),
                self._t("title_progress"))

            async def progress_cb(done, total):
                pct = round(done / total * 100) if total > 0 else 0
                _LOGGER.info(f"Category enrichment progress: {done}/{total} ({pct}%)")
                await self._notify(hass,
                    self._t("phase2_progress").format(done=done, total=total, pct=pct),
                    self._t("title_progress"))

            _LOGGER.info(f"Fetching categories for {total_products} products...")
            cat_map = await self._rohlik_api.fetch_product_categories_batch(uncategorized, progress_callback=progress_cb)
            async with self._store_lock:
                new_cats = self._order_store.update_product_categories(cat_map)
                stats["products_categorized"] = new_cats
                if new_cats > 0:
                    await self._order_store.async_save()
            _LOGGER.info(f"Categorized {new_cats} products")

        # Done - only notify if something was actually enriched this run.
        if stats["orders_enriched"] > 0 or stats["products_categorized"] > 0:
            await self._notify(hass,
                self._t("complete").format(
                    orders_enriched=stats["orders_enriched"],
                    products_categorized=stats["products_categorized"],
                    total_orders=self._order_store.alltime_count(),
                    enriched_orders=self._order_store.enriched_count,
                    products_in_cache=self._order_store.cached_product_count,
                ),
                self._t("title_complete"))

        self.async_update_listeners()
        return {
            "total_orders": self._order_store.alltime_count(),
            "enriched_orders": self._order_store.enriched_count,
            "unenriched_remaining": len(self._order_store.unenriched_order_ids),
            "products_in_cache": self._order_store.cached_product_count,
            "orders_enriched_this_run": stats["orders_enriched"],
            "products_categorized_this_run": stats["products_categorized"],
        }

    async def refresh_slots(self) -> None:
        """Cheaply refresh only the delivery-slot data (for express-slot polling).

        Updates self.data["next_delivery_slot"] in place and notifies entities,
        without disturbing the rest of the data or the regular refresh cycle.
        """
        if not self.data:
            return
        result = await self._rohlik_api.get_timeslots()
        if result is not None:
            self.data["next_delivery_slot"] = result
            self.async_update_listeners()

    async def async_close(self) -> None:
        """Release resources held by the API client (called on unload)."""
        await self._rohlik_api.async_close()

    # New service methods
    async def add_to_cart(self, product_id: int, quantity: int) -> Dict:
        """Add a product to the shopping cart."""
        product_list = [{"product_id": product_id, "quantity": quantity}]
        result = await self._rohlik_api.add_to_cart(product_list)
        await self.async_update()
        return result

    async def search_product(self, product_name: str, limit: int = 10, favourite: bool = False) -> Optional[Dict[str, Any]]:
        """Search for a product by name."""
        result = await self._rohlik_api.search_product(product_name, limit, favourite)
        return result

    async def get_shopping_list(self, shopping_list_id: str) -> Dict[str, Any]:
        """Get a shopping list by ID."""
        result = await self._rohlik_api.get_shopping_list(shopping_list_id)
        return result

    async def get_cart_content(self) -> Dict:
        """ Retrieves cart content. """
        result = await self._rohlik_api.get_cart_content()
        return result

    async def search_and_add(self, product_name: str, quantity: int, favourite: bool = False) -> Dict | None:
        """ Searches for product by name and adds to cart"""

        searched_product = await self.search_product(product_name, limit = 5, favourite=favourite)

        if searched_product:
            await self.add_to_cart(searched_product["search_results"][0]["id"], quantity)
            return {"success": True, "message": "", "added_to_cart": [searched_product["search_results"][0]]}

        else:
            return {"success": False, "message": f'No product matched when searching for "{product_name}"{' in favourites' if favourite else ''}.', "added_to_cart": []}

    async def delete_from_cart(self, order_field_id: str) -> Dict:
        """Delete a product from the shopping cart using orderFieldId."""
        result = await self._rohlik_api.delete_from_cart(order_field_id)
        await self.async_update()  # Refresh data after deletion
        return result