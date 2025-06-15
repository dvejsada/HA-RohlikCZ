from __future__ import annotations

from typing import List, Dict, Any

import logging
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, ATTR_CONFIG_ENTRY_ID, ATTR_PRODUCT_ID, ATTR_QUANTITY, ATTR_PRODUCT_NAME, \
    ATTR_SHOPPING_LIST_ID, ATTR_LIMIT, ATTR_FAVOURITE_ONLY, SERVICE_ADD_TO_CART, SERVICE_SEARCH_PRODUCT, SERVICE_GET_SHOPPING_LIST, \
    SERVICE_GET_CART_CONTENT, SERVICE_SEARCH_AND_ADD_PRODUCT, SERVICE_UPDATE_DATA

_LOGGER = logging.getLogger(__name__)

def register_services(hass: HomeAssistant) -> None:
    """Register services for the Rohlik integration."""

    async def async_add_to_cart_service(call: ServiceCall) -> List[int]:
        """Add product to cart service."""
        config_entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
        product_id = call.data[ATTR_PRODUCT_ID]
        quantity = call.data[ATTR_QUANTITY]

        if config_entry_id not in hass.data[DOMAIN]:
            raise HomeAssistantError(f"Config entry {config_entry_id} not found")

        account = hass.data[DOMAIN][config_entry_id]
        try:
            result = await account.add_to_cart(product_id, quantity)
            _LOGGER.info(f"Product added to cart for {account.name}: {result}")
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

        if config_entry_id not in hass.data[DOMAIN]:
            raise HomeAssistantError(f"Config entry {config_entry_id} not found")

        account = hass.data[DOMAIN][config_entry_id]
        try:
            # Create kwargs dictionary with only parameters that are not None
            kwargs = {}
            if limit:
                kwargs[ATTR_LIMIT] = limit
            if favourite:
                kwargs[ATTR_FAVOURITE_ONLY] = favourite

            result = await account.search_product(product_name, **kwargs)
            return result or {}
        except Exception as err:
            _LOGGER.error(f"Failed to search for product: {err}")
            raise HomeAssistantError(f"Failed to search for product: {err}")

    async def async_search_and_add_product_service(call: ServiceCall) -> Dict[str, Any]:
        """Search for a product and return results."""
        config_entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
        product_name = call.data[ATTR_PRODUCT_NAME]
        quantity = call.data[ATTR_QUANTITY]
        favourite = call.data.get(ATTR_FAVOURITE_ONLY, None)

        if config_entry_id not in hass.data[DOMAIN]:
            raise HomeAssistantError(f"Config entry {config_entry_id} not found")

        account = hass.data[DOMAIN][config_entry_id]
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

        if config_entry_id not in hass.data[DOMAIN]:
            raise HomeAssistantError(f"Config entry {config_entry_id} not found")

        account = hass.data[DOMAIN][config_entry_id]
        try:
            result = await account.get_shopping_list(shopping_list_id)
            return result
        except Exception as err:
            _LOGGER.error(f"Failed to get shopping list: {err}")
            raise HomeAssistantError(f"Failed to get shopping list: {err}")

    async def async_get_cart_service(call: ServiceCall) -> Dict[str, Any]:
        """Get shopping cart content."""
        config_entry_id = call.data[ATTR_CONFIG_ENTRY_ID]

        if config_entry_id not in hass.data[DOMAIN]:
            raise HomeAssistantError(f"Config entry {config_entry_id} not found")

        account = hass.data[DOMAIN][config_entry_id]
        try:
            result = await account.get_cart_content()
            return result
        except Exception as err:
            _LOGGER.error(f"Failed to get cart content: {err}")
            raise HomeAssistantError(f"Failed to get get cart content: {err}")

    async def async_update_data(call: ServiceCall) -> None:
        """Updates integration data."""
        config_entry_id = call.data[ATTR_CONFIG_ENTRY_ID]

        if config_entry_id not in hass.data[DOMAIN]:
            raise HomeAssistantError(f"Config entry {config_entry_id} not found")

        account = hass.data[DOMAIN][config_entry_id]
        try:
            await account.async_update()

        except Exception as err:
            raise HomeAssistantError(f"Failed to update data: {err}")


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