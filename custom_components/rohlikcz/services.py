from __future__ import annotations

from dataclasses import asdict
from typing import List, Dict, Any

import logging
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, ATTR_CONFIG_ENTRY_ID, ATTR_PRODUCT_ID, ATTR_QUANTITY, ATTR_PRODUCT_NAME, \
    ATTR_SHOPPING_LIST_ID, ATTR_LIMIT, ATTR_FAVOURITE_ONLY, SERVICE_ADD_TO_CART, SERVICE_SEARCH_PRODUCT, SERVICE_GET_SHOPPING_LIST, \
    SERVICE_GET_CART_CONTENT, SERVICE_SEARCH_AND_ADD_PRODUCT, SERVICE_UPDATE_DATA, SERVICE_FETCH_ORDER_HISTORY, SERVICE_ENRICH_ORDERS, \
    SERVICE_REFRESH_SLOTS

_LOGGER = logging.getLogger(__name__)

def _get_account(hass: HomeAssistant, config_entry_id: str):
    """Return the coordinator for a config entry id, or raise a validation error."""
    entry = hass.config_entries.async_get_entry(config_entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(f"Rohlik config entry {config_entry_id} not found")
    account = getattr(entry, "runtime_data", None)
    if account is None:
        raise ServiceValidationError(f"Rohlik config entry {config_entry_id} is not loaded")
    return account


def register_services(hass: HomeAssistant) -> None:
    """Register services for the Rohlik integration."""

    async def async_add_to_cart_service(call: ServiceCall) -> List[int]:
        """Add product to cart service."""
        config_entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
        product_id = call.data[ATTR_PRODUCT_ID]
        quantity = call.data[ATTR_QUANTITY]

        account = _get_account(hass, config_entry_id)
        try:
            result = await account.add_to_cart(product_id, quantity)
            _LOGGER.info(f"Product added to cart for {account.account_name}: {result}")
            return result
        except Exception as err:
            _LOGGER.error(f"Failed to add product to cart: {err}")
            raise HomeAssistantError(f"Failed to add product to cart: {err}")

    async def async_search_product_service(call: ServiceCall) -> Dict[str, Any]:
        """Search for a product and return results."""
        config_entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
        product_name = call.data[ATTR_PRODUCT_NAME]
        limit = call.data.get(ATTR_LIMIT, None)
        favourite = call.data.get(ATTR_FAVOURITE_ONLY, None)

        account = _get_account(hass, config_entry_id)
        try:
            # Create kwargs dictionary with only parameters that are not None
            kwargs = {}
            if limit:
                kwargs[ATTR_LIMIT] = limit
            if favourite:
                kwargs[ATTR_FAVOURITE_ONLY] = favourite

            result = await account.search_product(product_name, **kwargs)
            if not result:
                return {}
            return {"search_results": [asdict(item) for item in result.results]}
        except Exception as err:
            _LOGGER.error(f"Failed to search for product: {err}")
            raise HomeAssistantError(f"Failed to search for product: {err}")

    async def async_search_and_add_product_service(call: ServiceCall) -> Dict[str, Any]:
        """Search for a product and return results."""
        config_entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
        product_name = call.data[ATTR_PRODUCT_NAME]
        quantity = call.data[ATTR_QUANTITY]
        favourite = call.data.get(ATTR_FAVOURITE_ONLY, None)

        account = _get_account(hass, config_entry_id)
        try:
            # Create kwargs dictionary with only parameters that are not None
            kwargs = {}
            if favourite:
                kwargs['favourite'] = favourite

            # Unpack kwargs in the function call
            result = await account.search_and_add(product_name, quantity, **kwargs)
            return result or {}
        except Exception as err:
            _LOGGER.error(f"Failed to search for product: {err}")
            raise HomeAssistantError(f"Failed to search for product: {err}")


    async def async_get_shopping_list_service(call: ServiceCall) -> Dict[str, Any]:
        """Get shopping list by ID."""
        config_entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
        shopping_list_id = call.data[ATTR_SHOPPING_LIST_ID]

        account = _get_account(hass, config_entry_id)
        try:
            result = await account.get_shopping_list(shopping_list_id)
            return asdict(result)
        except Exception as err:
            _LOGGER.error(f"Failed to get shopping list: {err}")
            raise HomeAssistantError(f"Failed to get shopping list: {err}")

    async def async_get_cart_service(call: ServiceCall) -> Dict[str, Any]:
        """Get shopping cart content."""
        config_entry_id = call.data[ATTR_CONFIG_ENTRY_ID]

        account = _get_account(hass, config_entry_id)
        try:
            result = await account.get_cart_content()
            return asdict(result)
        except Exception as err:
            _LOGGER.error(f"Failed to get cart content: {err}")
            raise HomeAssistantError(f"Failed to get cart content: {err}")

    async def async_update_data(call: ServiceCall) -> None:
        """Updates integration data."""
        config_entry_id = call.data[ATTR_CONFIG_ENTRY_ID]

        account = _get_account(hass, config_entry_id)
        try:
            await account.async_update()

        except Exception as err:
            raise HomeAssistantError(f"Failed to update data: {err}")

    async def async_refresh_slots(call: ServiceCall) -> None:
        """Cheaply refresh only the delivery-slot data (express-slot polling)."""
        config_entry_id = call.data[ATTR_CONFIG_ENTRY_ID]

        account = _get_account(hass, config_entry_id)
        try:
            await account.refresh_slots()
        except Exception as err:
            raise HomeAssistantError(f"Failed to refresh slots: {err}")

    async def async_fetch_order_history(call: ServiceCall) -> None:
        """Fetch complete order history from Rohlik."""
        config_entry_id = call.data[ATTR_CONFIG_ENTRY_ID]

        account = _get_account(hass, config_entry_id)
        try:
            result = await account.fetch_full_order_history(hass=hass)
            _LOGGER.info(f"Fetch order history result: {result}")
        except Exception as err:
            _LOGGER.error(f"Failed to fetch order history: {err}")
            raise HomeAssistantError(f"Failed to fetch order history: {err}")

    async def async_enrich_orders(call: ServiceCall) -> None:
        """Enrich stored orders with item details and product categories."""
        config_entry_id = call.data[ATTR_CONFIG_ENTRY_ID]

        account = _get_account(hass, config_entry_id)
        try:
            result = await account.enrich_order_details(hass=hass)
            _LOGGER.info(f"Enrich orders result: {result}")
        except Exception as err:
            _LOGGER.error(f"Failed to enrich orders: {err}")
            raise HomeAssistantError(f"Failed to enrich orders: {err}")

    # Register the services
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_TO_CART,
        async_add_to_cart_service,
        schema=vol.Schema({
            vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
            vol.Required(ATTR_PRODUCT_ID): cv.positive_int,
            vol.Required(ATTR_QUANTITY, default=1): cv.positive_int,
        }),
        supports_response=True
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEARCH_PRODUCT,
        async_search_product_service,
        schema=vol.Schema({
            vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
            vol.Required(ATTR_PRODUCT_NAME): cv.string,
            vol.Optional(ATTR_LIMIT, default=10): cv.positive_int,
            vol.Optional(ATTR_FAVOURITE_ONLY, default=False): cv.boolean
        }),
        supports_response=True
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEARCH_AND_ADD_PRODUCT,
        async_search_and_add_product_service,
        schema=vol.Schema({
            vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
            vol.Required(ATTR_PRODUCT_NAME): cv.string,
            vol.Required(ATTR_QUANTITY): cv.positive_int,
            vol.Optional(ATTR_FAVOURITE_ONLY, default=False): cv.boolean
        }),
        supports_response=True
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_SHOPPING_LIST,
        async_get_shopping_list_service,
        schema=vol.Schema({
            vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
            vol.Required(ATTR_SHOPPING_LIST_ID): cv.string,
        }),
        supports_response=True
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_CART_CONTENT,
        async_get_cart_service,
        schema=vol.Schema({
            vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        }),
        supports_response=True
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_DATA,
        async_update_data,
        schema=vol.Schema({
            vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string
        }),
        supports_response=SupportsResponse.NONE
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_SLOTS,
        async_refresh_slots,
        schema=vol.Schema({
            vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string
        }),
        supports_response=SupportsResponse.NONE
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_FETCH_ORDER_HISTORY,
        async_fetch_order_history,
        schema=vol.Schema({
            vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        }),
        supports_response=SupportsResponse.NONE
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ENRICH_ORDERS,
        async_enrich_orders,
        schema=vol.Schema({
            vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        }),
        supports_response=SupportsResponse.NONE
    )


