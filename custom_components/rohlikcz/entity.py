from __future__ import annotations

from datetime import datetime

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .hub import RohlikAccount


class BaseEntity(CoordinatorEntity[RohlikAccount]):
    """Base class for entities in the Rohlík CZ integration.

    Built on CoordinatorEntity, so state updates are wired automatically when
    the coordinator refreshes - individual entities no longer register their
    own callbacks.
    """

    # NOTE: Do not set _attr_entity_name, it breaks localization!
    _attr_has_entity_name = True

    def __init__(self, rohlik_account: RohlikAccount) -> None:
        super().__init__(rohlik_account)

        if hasattr(self, "entity_description") and not self.translation_key:
            self._attr_translation_key = self.entity_description.key
        if self.translation_key is None:
            raise ValueError(f"translation_key is not set for {type(self).__name__}")

        self._rohlik_account = rohlik_account
        self._attr_device_info = rohlik_account.device_info
        self._attr_unique_id = f"{rohlik_account.unique_id}_{self.translation_key}"


class EtaTimedEntity(BaseEntity):
    """Entity whose state changes with the clock relative to the delivery ETA.

    Subclasses return the next moment their state changes from
    ``_next_change()``; one timer is kept armed for it and re-armed on every
    coordinator update.
    """

    _unsub_timer: CALLBACK_TYPE | None = None

    def _next_change(self) -> datetime | None:
        raise NotImplementedError

    def _before_write(self) -> None:
        """Update latched state before the state is written (no-op by default)."""

    @callback
    def _cancel_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    @callback
    def _schedule_update(self) -> None:
        self._cancel_timer()
        if (when := self._next_change()) is not None:
            self._unsub_timer = async_track_point_in_time(
                self.hass, self._handle_tick, when
            )

    @callback
    def _handle_tick(self, _now: datetime) -> None:
        self._unsub_timer = None
        self._before_write()
        self.async_write_ha_state()
        self._schedule_update()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel_timer)
        self._before_write()
        self._schedule_update()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._before_write()
        self.async_write_ha_state()
        self._schedule_update()
