from __future__ import annotations
from collections.abc import Callable
from typing import Any, cast, List, Optional, Dict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN
from .rohlik_api import RohlikCZAPI


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

    async def async_update(self) -> None:
        """ Updates the data from API."""

        self.data = await self._rohlik_api.get_data()

        await self.publish_updates()

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
        return result

    async def search_product(self, product_name: str) -> Optional[Dict[str, Any]]:
        """Search for a product by name."""
        result = await self._rohlik_api.search_product(product_name)
        return result

    async def get_shopping_list(self, shopping_list_id: str) -> Dict[str, Any]:
        """Get a shopping list by ID."""
        result = await self._rohlik_api.get_shopping_list(shopping_list_id)
        return result

    async def get_cart_content(self) -> Dict:
        """ Retrieves cart content. """
        result = await self._rohlik_api.get_cart_content()
        return result

    async def search_and_add(self, product_name: str, quantity: int) -> Dict:
        """ Searches for product by name and adds to cart"""
        searched_product = await self.search_product(product_name)
        added_product: dict = await self.add_to_cart(searched_product["id"], quantity)

        return added_product