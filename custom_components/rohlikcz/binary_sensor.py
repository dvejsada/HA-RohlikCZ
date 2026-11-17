"""Platform for binary sensor."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .const import DOMAIN, ICON_REUSABLE, ICON_PARENTCLUB, ICON_PREMIUM, ICON_ORDER, ICON_TIMESLOT, ICON_CALENDAR_CHECK, \
    ICON_CALENDAR_REMOVE, ICON_LAST_MILE, ICON_LAST_MILE_OFF
from .entity import BaseEntity
from .hub import RohlikAccount
from .utils import get_earliest_order

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    """Add sensors for passed config_entry in HA."""
    rohlik_account: RohlikAccount = config_entry.runtime_data
    async_add_entities([
        IsReusableSensor(rohlik_account),
        IsParentSensor(rohlik_account),
        IsPremiumSensor(rohlik_account),
        IsOrderedSensor(rohlik_account),
        IsReservedSensor(rohlik_account),
        IsExpressAvailable(rohlik_account),
        LastMileSensor(rohlik_account),
    ])

#: Last Mile turns off at the latest this long after the ETA.
LAST_MILE_MAX_OVERDUE = timedelta(minutes=60)


class LastMileSensor(BaseEntity, BinarySensorEntity):
    """On from the configured lead time before the courier's ETA until delivery.

    Only an ETA from a courier announcement counts: the booked slot alone says
    nothing about where the courier is. It turns off when the order is
    delivered (no longer upcoming) or, at the latest, an hour after the ETA.
    """

    _attr_translation_key = "last_mile"
    _attr_should_poll = False

    def __init__(self, rohlik_account: RohlikAccount) -> None:
        super().__init__(rohlik_account)
        self._unsub_timer: CALLBACK_TYPE | None = None

    def _window(self) -> tuple[datetime, datetime] | None:
        tracker = self._rohlik_account.eta
        if tracker.eta is None or tracker.order_id is None or not tracker.is_live:
            return None
        return (
            tracker.eta - self._rohlik_account.last_mile_lead,
            tracker.eta + LAST_MILE_MAX_OVERDUE,
        )

    @property
    def is_on(self) -> bool:
        if (window := self._window()) is None:
            return False
        now = dt_util.utcnow().timestamp()
        return window[0].timestamp() <= now <= window[1].timestamp()

    @property
    def available(self) -> bool:
        # One failed refresh must not flip on -> unavailable -> on and re-fire
        # automations (e.g. opening the door) while a live ETA is known.
        return super().available or self._rohlik_account.eta.is_live

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        tracker = self._rohlik_account.eta
        return {
            "eta": tracker.eta if tracker.is_live else None,
            "lead_minutes": int(self._rohlik_account.last_mile_lead.total_seconds() // 60),
        }

    @property
    def icon(self) -> str:
        return ICON_LAST_MILE if self.is_on else ICON_LAST_MILE_OFF

    @callback
    def _schedule_update(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
        if (window := self._window()) is None:
            return
        now = dt_util.utcnow().timestamp()
        start, end = window
        if now < start.timestamp():
            when = start
        elif now <= end.timestamp():
            when = end + timedelta(seconds=1)
        else:
            return
        self._unsub_timer = async_track_point_in_time(self.hass, self._handle_tick, when)

    @callback
    def _handle_tick(self, _now: datetime) -> None:
        self._unsub_timer = None
        self.async_write_ha_state()
        self._schedule_update()

    @callback
    def _cancel_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel_timer)
        self._schedule_update()

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
        self._schedule_update()

class IsExpressAvailable(BaseEntity, BinarySensorEntity):
    _attr_translation_key = "is_express_available"
    _attr_should_poll = False

    @property
    def is_on(self) -> bool | None:
        slot_data = (self._rohlik_account.data.get("next_delivery_slot") or {}).get('data') or {}
        express_slot = slot_data.get('expressSlot')
        if not express_slot:
            return False
        capacity = (express_slot.get("timeSlotCapacityDTO") or {}).get("totalFreeCapacityPercent", 0)
        if int(capacity) == 0:
            return False
        return True

    @property
    def icon(self) -> str:
        if self.is_on:
            return ICON_CALENDAR_CHECK
        else:
            return ICON_CALENDAR_REMOVE


class IsReusableSensor(BaseEntity, BinarySensorEntity):
    """Sensor to say whether the user use reusable bags."""

    _attr_translation_key = "is_reusable"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    @property
    def is_on(self) -> bool | None:
        return self._rohlik_account.data.get('login', {}).get('data', {}).get('user', {}).get('reusablePackaging', False)

    @property
    def icon(self) -> str:
        return ICON_REUSABLE


class IsParentSensor(BaseEntity, BinarySensorEntity):
    """Sensor for whether the user is a member of the parent club."""

    _attr_translation_key = "is_parent"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    @property
    def is_on(self) -> bool | None:
        return self._rohlik_account.data.get('login', {}).get('data', {}).get('user', {}).get('parentsClub', False)

    @property
    def icon(self) -> str:
        return ICON_PARENTCLUB


class IsPremiumSensor(BaseEntity, BinarySensorEntity):
    """Sensor for whether the user has premium membership."""

    _attr_translation_key = "is_premium"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    @property
    def is_on(self) -> bool | None:
        return (self._rohlik_account.data.get('login', {}).get('data', {}).get('user', {}).get('premium') or {}).get('active', False)

    @property
    def extra_state_attributes(self) -> dict | None:
        premium_data = self._rohlik_account.data.get('login', {}).get('data', {}).get('user', {}).get('premium') or {}
        if premium_data:
            return {
                "type": premium_data.get('premiumMembershipType'),
                "payment_type": premium_data.get('premiumType'),
                "expiration_date": premium_data.get('recurrentPaymentDate'),
                "remaining_days": premium_data.get('remainingDays'),
                "start_date": premium_data.get('startDate'),
                "end_date": premium_data.get('endDate'),
                "remaining_orders_without_limit": (premium_data.get('premiumLimits') or {}).get('ordersWithoutPriceLimit', {}).get('remaining'),
                "remaining_free_express": (premium_data.get('premiumLimits') or {}).get('freeExpressLimit', {}).get('remaining')
            }
        return None

    @property
    def icon(self) -> str:
        return ICON_PREMIUM


class IsOrderedSensor(BaseEntity, BinarySensorEntity):
    """Sensor for whether the next order is scheduled."""

    _attr_translation_key = "is_ordered"
    _attr_should_poll = False

    @property
    def is_on(self) -> bool | None:
        # Check if there's at least one order in the next_order list
        return len(self._rohlik_account.data.get('next_order', [])) > 0

    @property
    def extra_state_attributes(self) -> dict | None:
        next_orders = self._rohlik_account.data.get('next_order', [])
        order = get_earliest_order(next_orders)
        if order:
            return {
                "order_data": order
            }
        return None

    @property
    def icon(self) -> str:
        return ICON_ORDER


class IsReservedSensor(BaseEntity, BinarySensorEntity):
    """Sensor for whether a timeslot is reserved."""

    _attr_translation_key = "is_reserved"
    _attr_should_poll = False

    @property
    def is_on(self) -> bool | None:
        return ((self._rohlik_account.data.get('timeslot') or {}).get('data') or {}).get('active', False)

    @property
    def extra_state_attributes(self) -> dict | None:
        timeslot_data = ((self._rohlik_account.data.get('timeslot') or {}).get('data') or {}).get('reservationDetail', {})
        if timeslot_data:
            return timeslot_data
        return None

    @property
    def icon(self) -> str:
        return ICON_TIMESLOT
