"""Platform for the courier's live position on the map."""
from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ICON_COURIER
from .entity import CourierEntity
from .hub import RohlikAccount


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the courier tracker for the passed config entry."""
    rohlik_hub: RohlikAccount = config_entry.runtime_data
    async_add_entities([CourierTracker(rohlik_hub)])


class CourierTracker(CourierEntity, TrackerEntity):
    """The courier's last reported position while they are on their way to you.

    The position is a third party's location and ends up in the recorder
    history, so the entity is disabled by default. Without a current
    position (no delivery under way, the shop flags it as stale, or it is
    more than 5 minutes old) the state is unknown and the entity is not
    shown on the map.
    """

    _attr_translation_key = "courier"
    _attr_should_poll = False
    _attr_entity_registry_enabled_default = False
    _attr_source_type = SourceType.GPS
    # TrackerEntity defaults to the diagnostic category, which would hide the
    # courier from auto-generated dashboards.
    _attr_entity_category = None

    @property
    def force_update(self) -> bool:
        # TrackerEntity forces every write for non-polling trackers, which
        # would record an identical "unknown" state on every refresh.
        return False

    @property
    def latitude(self) -> float | None:
        position = self._rohlik_account.courier_position
        return position.latitude if position is not None else None

    @property
    def longitude(self) -> float | None:
        position = self._rohlik_account.courier_position
        return position.longitude if position is not None else None

    @property
    def location_accuracy(self) -> float:
        # The shop reports no accuracy; 0 keeps HA's zone check on the point.
        return 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        position = self._rohlik_account.courier_position
        return {
            "order_id": self._rohlik_account.tracking_order_id,
            "position_recorded": position.recorded_at if position is not None else None,
        }

    @property
    def icon(self) -> str:
        return ICON_COURIER
