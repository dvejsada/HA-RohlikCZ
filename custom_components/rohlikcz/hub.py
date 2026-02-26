from __future__ import annotations
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


class RohlikAccount:
    """Setting RohlikCZ account as device."""

    def __init__(self, hass: HomeAssistant, username: str, password: str) -> None:
        """Initialize account info."""
        super().__init__()
        self._hass = hass
        self._username: str = username
        self._password: str = password
        self._rohlik_api = RohlikCZAPI(self._username, self._password)
        self.data: dict = {}
        self._callbacks: set[Callable[[], None]] = set()
        self._order_store: OrderStore | None = None

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

        # Initialize order store on first update (need user_id from login)
        if not self._order_store and self.data.get("login"):
            user_id = str(self.data["login"]["data"]["user"]["id"])
            storage_dir = self._hass.config.path(".storage")
            self._order_store = OrderStore(storage_dir, user_id)

        # Process delivered orders into persistent store
        if self._order_store and self.data.get("delivered_orders"):
            self._order_store.process_orders(self.data["delivered_orders"])

        await self.publish_updates()

    async def fetch_full_order_history(self) -> int:
        """Fetch all historical orders and store them. Returns total order count."""
        all_orders = await self._rohlik_api.fetch_all_delivered_orders()
        if self._order_store and all_orders:
            new = self._order_store.process_orders(all_orders)
            await self.publish_updates()
            return self._order_store.alltime_count()
        return 0

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