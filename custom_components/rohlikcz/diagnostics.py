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
    # Coordinates, courier and address details, also in nested API payloads.
    "latitude",
    "longitude",
    "displayName",
    "phoneNumber",
    "fullAddress",
    "display",
    "postalCode",
    # Delivery announcements add the courier's name and phone number here.
    "additionalContent",
}


def _tracking_diagnostics(account: Any) -> dict[str, Any] | None:
    """The hub's delivery tracking state, without courier name or position."""
    tracked = account.tracked_order
    if tracked is None:
        return None
    tracking = tracked.tracking
    progress = tracking.progress

    def iso(value: Any) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "order_id": tracked.order_id,
        "valid": account.tracking is not None,
        "status": tracking.status,
        "estimated_arrival": iso(tracking.estimated_arrival),
        "stops_before": tracking.stops_before,
        "generated_at": iso(progress.generated_at if progress else None),
        "slot_since": iso(tracking.slot_since),
        "early_delivery_available": tracking.early_delivery_available,
        "early_delivery_requested": tracking.early_delivery_requested,
        "early_delivery_allowed_from": iso(tracking.early_delivery_allowed_from),
        "fetched_at": iso(tracked.fetched_at),
        "not_upcoming_since": iso(tracked.not_upcoming_since),
        "delivered_at": iso(tracked.delivered_at),
        "final_fetches": tracked.final_fetches,
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
            "delivery_eta": account.eta.as_diagnostics(),
            "delivery_tracking": _tracking_diagnostics(account),
            "orders_without_tracking": len(account._no_tracking),
        },
        "data": async_redact_data(account.data or {}, TO_REDACT),
    }
