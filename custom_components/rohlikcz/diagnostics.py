"""Diagnostics support for the Rohlik.cz integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import RohlikConfigEntry

# Keys whose values may contain personal data and are redacted from diagnostics.
TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    "email",
    "password",
    "phone",
    "name",
    "firstName",
    "lastName",
    "address",
    "street",
    "city",
    "houseNumber",
    "zip",
    "gps",
    "lat",
    "lon",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RohlikConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    account = entry.runtime_data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {
            "last_update_success": account.last_update_success,
            "update_interval": str(account.update_interval),
            "analytics": account.analytics,
            "has_address": account.has_address,
        },
        "data": async_redact_data(account.data or {}, TO_REDACT),
    }
