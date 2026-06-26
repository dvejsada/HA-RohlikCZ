from __future__ import annotations
import asyncio
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast, List, Optional, Dict
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN
from .rohlik_api import RohlikCZAPI

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


class OrderStore:
    """Persistent storage for order history in HA's .storage directory."""

    def __init__(self, storage_dir: str, user_id: str, hass: HomeAssistant):
        self._path = os.path.join(storage_dir, f"rohlikcz_{user_id}_orders.json")
        self._data = {"version": 3, "user_id": user_id, "tracking_since": None, "backfill_complete": False, "orders": {}, "product_categories": {}}
        self._hass = hass

    @classmethod
    async def async_create(cls, storage_dir: str, user_id: str, hass: HomeAssistant) -> "OrderStore":
        """Create and load an OrderStore asynchronously."""
        store = cls(storage_dir, user_id, hass)
        await hass.async_add_executor_job(store._load_sync)
        return store

    def _load_sync(self) -> None:
        """Load order store from disk (blocking, run in executor)."""
        if os.path.exists(self._path):
            try:
                with open(self._path, "r") as f:
                    self._data = json.load(f)
                migrated = False
                # Migrate v1 → v2
                if self._data.get("version", 1) < 2:
                    self._data["version"] = 2
                    if "product_categories" not in self._data:
                        self._data["product_categories"] = {}
                    migrated = True
                    _LOGGER.info("Migrated order store from v1 to v2")
                elif "product_categories" not in self._data:
                    self._data["product_categories"] = {}
                # Migrate v2 → v3 (add backfill_complete flag)
                if self._data.get("version", 1) < 3:
                    self._data["version"] = 3
                    # Existing stores with tracking_since already set had a
                    # successful backfill in a prior run; mark it complete so
                    # we don't re-download everything on next restart.
                    if "backfill_complete" not in self._data:
                        self._data["backfill_complete"] = self._data.get("tracking_since") is not None
                    migrated = True
                    _LOGGER.info("Migrated order store from v2 to v3")
                if migrated:
                    self._save_sync()
            except (json.JSONDecodeError, OSError) as err:
                _LOGGER.error(f"Failed to load order store: {err}")

    def _save_sync(self) -> None:
        """Save order store to disk (blocking, run in executor)."""
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2)
        except OSError as err:
            _LOGGER.error(f"Failed to save order store: {err}")

    async def async_save(self) -> None:
        """Save order store to disk asynchronously."""
        await self._hass.async_add_executor_job(self._save_sync)

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


class RohlikAccount:
    """Setting RohlikCZ account as device."""

    def __init__(self, hass: HomeAssistant, username: str, password: str, analytics: list[str] | None = None, top_n: int = 10, hide_discontinued: bool = True) -> None:
        """Initialize account info."""
        super().__init__()
        self._hass = hass
        self._username: str = username
        self._password: str = password
        self._rohlik_api = RohlikCZAPI(self._username, self._password)
        self.data: dict = {}
        self._callbacks: set[Callable[[], None]] = set()
        self._order_store: OrderStore | None = None
        self._store_lock = asyncio.Lock()
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
    def has_address(self):
        if self.data["next_delivery_slot"]:
            return True
        else:
            return False

    @property
    def device_info(self) -> DeviceInfo:
        """ Provides a device info. """
        return {"identifiers": {(DOMAIN, self.data["login"]["data"]["user"]["id"])}, "name": self.data["login"]["data"]["user"]["name"], "manufacturer": "Rohlík.cz"}

    @property
    def name(self) -> str:
        """Provides name for account."""
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

    async def async_update(self) -> None:
        """ Updates the data from API."""

        self.data = await self._rohlik_api.get_data()

        # Initialize order store on first update (only if analytics enabled)
        if self._analytics and not self._order_store and self.data.get("login"):
            user_id = str(self.data["login"]["data"]["user"]["id"])
            storage_dir = self._hass.config.path(".storage")
            self._order_store = await OrderStore.async_create(storage_dir, user_id, self._hass)

        # Process delivered orders into persistent store and auto-enrich new ones
        if self._analytics and self._order_store and self.data.get("delivered_orders"):
            async with self._store_lock:
                new = self._order_store.process_orders(self.data["delivered_orders"])
                if new > 0:
                    await self._order_store.async_save()
            if new > 0:
                # Schedule enrichment in background (don't block update cycle / setup)
                self._hass.async_create_task(self._auto_enrich_new_orders(new))

        await self.publish_updates()

    async def _auto_enrich_new_orders(self, new_count: int) -> None:
        """Auto-enrich recently added orders in the background."""
        enriched = False
        try:
            async with self._store_lock:
                unenriched = self._order_store.unenriched_order_ids
                recent_unenriched = unenriched[-new_count:] if len(unenriched) >= new_count else unenriched
                if recent_unenriched:
                    items_map = await self._rohlik_api.enrich_orders_with_items(recent_unenriched)
                    for order_id, items in items_map.items():
                        self._order_store.add_items_to_order(order_id, items)
                    uncategorized = self._order_store.uncategorized_product_ids()
                    if uncategorized:
                        cat_map = await self._rohlik_api.fetch_product_categories_batch(uncategorized)
                        self._order_store.update_product_categories(cat_map)
                    if items_map:
                        await self._order_store.async_save()
                        _LOGGER.info(f"Auto-enriched {len(items_map)} new orders")
                        enriched = True
            if enriched:
                await self.publish_updates()
        except Exception as err:
            _LOGGER.warning(f"Auto-enrichment of new orders failed: {err}")

    async def fetch_full_order_history(self, hass=None) -> dict:
        """Fetch all historical orders, enrich with items, and categorize products.

        Returns dict with stats.
        """
        async with self._store_lock:
            # Step 1: Fetch order list (existing behavior)
            all_orders = await self._rohlik_api.fetch_all_delivered_orders()
            new_orders = 0
            if self._order_store and all_orders:
                new_orders = self._order_store.process_orders(all_orders)
                if new_orders > 0:
                    await self._order_store.async_save()

            # Step 2: Enrich with items and categories
            if self._order_store:
                enrich_stats = await self._enrich_order_details(hass=hass)
                enrich_stats["new_orders"] = new_orders
                # Mark backfill as done so we don't re-download on next restart
                if not self._order_store.backfill_complete:
                    self._order_store.mark_backfill_complete()
                    await self._order_store.async_save()
                return enrich_stats

            return {"total_orders": 0, "new_orders": 0}

    def _t(self, key: str) -> str:
        """Get localized notification string based on HA language."""
        lang = self._hass.config.language or "en"
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

        Public entry point — acquires the store lock.
        """
        async with self._store_lock:
            return await self._enrich_order_details(hass=hass)

    async def _enrich_order_details(self, hass=None) -> dict:
        """Internal enrichment logic — caller must hold _store_lock.

        Two-phase enrichment:
        1. Fetch items for orders missing them
        2. Fetch categories for products not yet in cache

        Returns stats dict.
        """
        if not self._order_store:
            return {"error": "Order store not initialized"}

        stats = {"orders_enriched": 0, "products_categorized": 0, "errors": 0}

        # Phase 1: Fetch items for unenriched orders
        unenriched = self._order_store.unenriched_order_ids
        if unenriched:
            await self._notify(hass,
                self._t("phase1").format(count=len(unenriched)),
                self._t("title_progress"))
            _LOGGER.info(f"Enriching {len(unenriched)} orders with item details...")
            items_map = await self._rohlik_api.enrich_orders_with_items(unenriched)
            for order_id, items in items_map.items():
                if self._order_store.add_items_to_order(order_id, items):
                    stats["orders_enriched"] += 1
            if stats["orders_enriched"] > 0:
                await self._order_store.async_save()
                _LOGGER.info(f"Added items to {stats['orders_enriched']} orders")

        # Phase 2: Fetch categories for uncategorized products
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
            new_cats = self._order_store.update_product_categories(cat_map)
            stats["products_categorized"] = new_cats
            if new_cats > 0:
                await self._order_store.async_save()
            _LOGGER.info(f"Categorized {new_cats} products")

        # Done
        await self._notify(hass,
            self._t("complete").format(
                orders_enriched=stats["orders_enriched"],
                products_categorized=stats["products_categorized"],
                total_orders=self._order_store.alltime_count(),
                enriched_orders=self._order_store.enriched_count,
                products_in_cache=self._order_store.cached_product_count,
            ),
            self._t("title_complete"))

        await self.publish_updates()
        return {
            "total_orders": self._order_store.alltime_count(),
            "enriched_orders": self._order_store.enriched_count,
            "unenriched_remaining": len(self._order_store.unenriched_order_ids),
            "products_in_cache": self._order_store.cached_product_count,
            "orders_enriched_this_run": stats["orders_enriched"],
            "products_categorized_this_run": stats["products_categorized"],
        }

    def register_callback(self, callback: Callable[[], None]) -> None:
        """Register callback, called when there are new data."""
        self._callbacks.add(callback)

    def remove_callback(self, callback: Callable[[], None]) -> None:
        """Remove previously registered callback."""
        self._callbacks.discard(callback)

    async def publish_updates(self) -> None:
        """Schedule call to all registered callbacks."""
        for callback in self._callbacks:
            callback()

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