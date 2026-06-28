"""Todo platform for Rohlik.cz integration."""
from __future__ import annotations

import logging
import re

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, ICON_CART
from .hub import RohlikAccount

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Rohlik shopping cart todo platform config entry."""
    rohlik_hub: RohlikAccount = config_entry.runtime_data

    async_add_entities([RohlikCartTodo(rohlik_hub)])


class RohlikCartTodo(CoordinatorEntity[RohlikAccount], TodoListEntity):
    """A Rohlik Shopping Cart TodoListEntity."""

    _attr_has_entity_name = True
    _attr_supported_features = (TodoListEntityFeature.CREATE_TODO_ITEM | TodoListEntityFeature.DELETE_TODO_ITEM)
    _attr_translation_key = "shopping_cart"
    _attr_icon = ICON_CART

    def __init__(
        self,
        rohlik_hub: RohlikAccount
    ) -> None:
        """Initialize RohlikCartTodo."""
        super().__init__(rohlik_hub)
        self._rohlik_hub = rohlik_hub
        self._attr_unique_id = f"{rohlik_hub.unique_id}-cart"
        self._attr_name = "Rohlik Shopping Cart"
        self._attr_device_info = rohlik_hub.device_info

    @property
    def todo_items(self) -> list[TodoItem] | None:
        """Handle updated data from the hub."""
        cart = self._rohlik_hub.data["cart"]

        if not cart:
            return None

        items = []
        for product in cart.products:
            # Format the summary to include relevant information
            summary = f"{product.name} ({product.quantity}) - {product.price} Kč"

            # Use cart_item_id as the unique identifier for cart items
            items.append(
                TodoItem(
                    summary=summary,
                    uid=str(product.cart_item_id),
                    status=TodoItemStatus.NEEDS_ACTION,
                    description=f"Category: {product.category_name}\n"
                               f"Brand: {product.brand}\n"
                               f"Product ID: {product.id}"
                )
            )

        return items

    async def async_create_todo_item(self, item: TodoItem) -> None:
        """Add item to shopping cart.

        Supports two input formats:
        - "product name" (quantity defaults to 1)
        - "X product name" (where X is the desired quantity)
        """

        # Check if the summary starts with a number followed by a space
        quantity_match = re.match(r'^(\d+)\s+(.+)$', item.summary)

        if quantity_match:
            # If format is "X product name"
            quantity = int(quantity_match.group(1))
            product_name = quantity_match.group(2)
        else:
            # If format is just "product name"
            quantity = 1
            product_name = item.summary

        # If there's still quantity info in parentheses, use that instead This handles cases like "rohlík (3)" or "2 rohlíky (5)" where (5) would take precedence
        parentheses_match = re.search(r'\((\d+)\)$', product_name)
        if parentheses_match:
            quantity = int(parentheses_match.group(1))
            product_name = product_name.split('(')[0].strip()

        # Search for product and add to cart
        result = await self._rohlik_hub.search_and_add(product_name, quantity)

        if not result or not result.get("success", False):
            _LOGGER.error("Failed to add product '%s' to cart", product_name)
            raise ServiceValidationError(f"Product not found: {product_name}")


    async def async_delete_todo_items(self, uids: list[str]) -> None:
        """Delete items from the shopping cart using the dedicated delete endpoint."""
        for uid in uids:
            try:
                # Call the new delete_from_cart method with the cart_item_id
                await self._rohlik_hub.delete_from_cart(uid)
                _LOGGER.debug("Deleted item: %s", uid)
            except Exception as err:
                _LOGGER.error("Error deleting item %s: %s", uid, err)
