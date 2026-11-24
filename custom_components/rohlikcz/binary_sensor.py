"""Platform for binary sensor."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, STATE_UNKNOWN
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, ICON_REUSABLE, ICON_PARENTCLUB, ICON_PREMIUM, ICON_ORDER, ICON_TIMESLOT, ICON_CALENDAR_CHECK, \
    ICON_CALENDAR_REMOVE, ICON_LAST_MILE, ICON_LAST_MILE_OFF, ICON_COURIER_AT_DOOR, ICON_COURIER_AWAY
from .entity import BaseEntity, EtaTimedEntity
from .eta import MAX_OVERDUE
from .hub import COURIER_COMING, RohlikAccount
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
        CourierAtDoorSensor(rohlik_account),
    ])


class TrackingLatch(EtaTimedEntity, RestoreEntity):
    """A per-order latch on live delivery tracking.

    Once set for an order it holds until that order is delivered, its tracking
    is no longer valid (stale, expired or another order), or MAX_OVERDUE has
    passed. It is restored after a restart while the same order is still
    being delivered, so a reload never turns it off and on again.
    """

    _latched_order: int | None = None
    _latch_until: datetime | None = None

    def _should_latch(self, now: datetime) -> bool:
        raise NotImplementedError

    def _latch_deadline(self, now: datetime) -> datetime:
        tracking = self._rohlik_account.tracking
        estimate = tracking.estimated_arrival if tracking is not None else None
        return max(estimate or now, now) + MAX_OVERDUE

    def _before_write(self) -> None:
        now = dt_util.utcnow()
        tracking = self._rohlik_account.tracking
        if self._latched_order is not None and (
            tracking is None
            or tracking.order_id != self._latched_order
            or tracking.is_delivered
            or self._latch_until is None
            or now > self._latch_until
        ):
            self._latched_order = self._latch_until = None
        if self._latched_order is None and tracking is not None and self._should_latch(now):
            self._latched_order = tracking.order_id
            self._latch_until = self._latch_deadline(now)

    @property
    def _latched(self) -> bool:
        return self._latched_order is not None

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        tracking = self._rohlik_account.tracking
        if (
            last is not None
            and last.state == STATE_ON
            and tracking is not None
            and tracking.status in COURIER_COMING
            and last.attributes.get("order_id") == tracking.order_id
        ):
            self._latched_order = tracking.order_id
            self._latch_until = self._latch_deadline(dt_util.utcnow())
        await super().async_added_to_hass()

    def _next_change(self) -> datetime | None:
        times = []
        if self._latch_until is not None:
            times.append(self._latch_until + timedelta(seconds=1))
        if (valid_until := self._rohlik_account.tracking_valid_until) is not None:
            times.append(valid_until + timedelta(seconds=1))
        return min(times) if times else None


class CourierAtDoorSensor(TrackingLatch, BinarySensorEntity):
    """On once the courier has arrived (ARRIVED / HANDING_OVER) until delivery.

    Latched per order, and available while it is on, so it is a safe
    automation trigger: a failed refresh, a reload or a restart does not make
    it go off and on again.
    """

    _attr_translation_key = "courier_at_door"
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.PRESENCE

    def _should_latch(self, now: datetime) -> bool:
        tracking = self._rohlik_account.tracking
        return tracking is not None and tracking.has_arrived

    @property
    def is_on(self) -> bool:
        return self._latched

    @property
    def available(self) -> bool:
        return super().available or self._latched or self._rohlik_account.tracking is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"order_id": self._latched_order}

    @property
    def icon(self) -> str:
        return ICON_COURIER_AT_DOOR if self.is_on else ICON_COURIER_AWAY

class LastMileSensor(TrackingLatch, BinarySensorEntity):
    """On from the configured lead time before the courier's ETA until delivery.

    Only a courier estimate counts (tracking or announcement): the booked slot
    alone says nothing about where the courier is. It also turns on when the
    courier arrives, and off as soon as the order is delivered or, at the
    latest, an hour after the ETA.

    Once tracking shows the courier on the way and the sensor is on, it is
    latched for that order: the courier's estimate shifting by a few minutes
    (observed while on the way) no longer turns it off and on again.
    """

    _attr_translation_key = "last_mile"
    _attr_should_poll = False

    def _window(self) -> tuple[datetime, datetime] | None:
        tracker = self._rohlik_account.eta
        if not tracker.is_live:
            return None
        return tracker.window(self._rohlik_account.last_mile_lead, MAX_OVERDUE)

    def _window_on(self, now: datetime) -> bool:
        if (window := self._window()) is None:
            return False
        return window[0] <= now <= window[1]

    def _should_latch(self, now: datetime) -> bool:
        tracking = self._rohlik_account.tracking
        if tracking is None or tracking.status not in COURIER_COMING:
            return False
        return tracking.has_arrived or self._window_on(now)

    @property
    def is_on(self) -> bool:
        tracking = self._rohlik_account.tracking
        if tracking is not None and tracking.is_delivered:
            return False
        if self._latched:
            return True
        now = dt_util.utcnow()
        return self._window_on(now) or (tracking is not None and tracking.has_arrived)

    @property
    def available(self) -> bool:
        # A failed refresh must not flip on -> unavailable -> on and re-fire
        # automations (e.g. opening the door) while a courier ETA is known.
        return super().available or self._rohlik_account.eta.is_live or self._latched

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        tracker = self._rohlik_account.eta
        tracking = self._rohlik_account.tracking
        return {
            "eta": tracker.eta if tracker.is_live else None,
            "lead_minutes": int(self._rohlik_account.last_mile_lead.total_seconds() // 60),
            "order_id": self._latched_order or (tracking.order_id if tracking else None),
        }

    @property
    def icon(self) -> str:
        return ICON_LAST_MILE if self.is_on else ICON_LAST_MILE_OFF

    def _next_change(self) -> datetime | None:
        times = []
        if (latch_change := super()._next_change()) is not None:
            times.append(latch_change)
        if (window := self._window()) is not None:
            now = dt_util.utcnow()
            start, end = window
            if now < start:
                times.append(start)
            elif now <= end:
                times.append(end + timedelta(seconds=1))
        return min(times) if times else None


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
        # next_order is None when the upcoming-orders request failed.
        return len(self._rohlik_account.data.get('next_order') or []) > 0

    @property
    def extra_state_attributes(self) -> dict | None:
        next_orders = self._rohlik_account.data.get('next_order') or []
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
