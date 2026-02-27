"""Rohlík CZ custom component."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN, CONF_ANALYTICS, DEFAULT_ANALYTICS,
    CONF_TOP_N, DEFAULT_TOP_N, CONF_HIDE_DISCONTINUED, DEFAULT_HIDE_DISCONTINUED,
)
from .hub import RohlikAccount
from .services import register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor", "binary_sensor", "todo", "calendar"]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries to new format."""
    _LOGGER.debug("Migrating from version %s", entry.version)

    if entry.version < 1:
        # Pre-analytics entries: set empty analytics (opt-in)
        new_options = {**entry.options, CONF_ANALYTICS: DEFAULT_ANALYTICS}
        hass.config_entries.async_update_entry(entry, options=new_options, version=1)
        _LOGGER.info("Migrated config entry to version 1 (analytics disabled by default)")

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Rohlik integration from a config entry flow."""
    analytics = entry.options.get(CONF_ANALYTICS, DEFAULT_ANALYTICS)

    top_n = int(entry.options.get(CONF_TOP_N, DEFAULT_TOP_N))
    hide_discontinued = entry.options.get(CONF_HIDE_DISCONTINUED, DEFAULT_HIDE_DISCONTINUED)
    rohlik_hub = RohlikAccount(hass, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD], analytics=analytics, top_n=top_n, hide_discontinued=hide_discontinued)
    await rohlik_hub.async_update()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = rohlik_hub

    # Register services
    register_services(hass)

    _LOGGER.info("Setting up platforms: %s", PLATFORMS)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.info("Platforms setup complete")

    # If analytics enabled, fetch full order history + enrich in background
    if analytics:
        async def _fetch_history():
            try:
                if rohlik_hub.order_store:
                    await rohlik_hub.fetch_full_order_history(hass=hass)
            except Exception as err:
                _LOGGER.error("Background order history fetch failed: %s", err)

        entry.async_create_background_task(hass, _fetch_history(), "rohlik_fetch_history")

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
