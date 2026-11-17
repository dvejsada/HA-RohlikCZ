"""Delivery ETA tracking shared by the delivery-time entities.

The precise delivery time comes from Rohlík's delivery announcement. Turning it
into a single ETA needs memory (the announcement is cleared shortly before the
courier arrives, and may concern a later concurrent order), so the result is
kept here, on the coordinator, rather than inside one entity. Delivery Time,
Delivery Time Remaining, Delivery Last Mile and the automatic ETA refresh all
read the same value.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .utils import extract_delivery_datetime, get_earliest_order, parse_delivery_datetime_string

_LOGGER = logging.getLogger(__name__)

#: A live ETA is kept at most this long after it has passed.
LIVE_ETA_STALE_AFTER = timedelta(hours=1)

#: Delivery Time Remaining and Delivery Last Mile give up this long after the ETA.
MAX_OVERDUE = timedelta(minutes=60)

#: Storage schema version for the ETA tracker.
ETA_STORE_VERSION = 1

#: Delay before writing tracker state to disk (coalesces frequent updates).
_SAVE_DELAY = 10

#: Where the current ETA came from.
EtaSource = Literal["announcement", "slot", "last_known"]


def eta_storage_key(entry_id: str) -> str:
    """Return the storage key of a config entry's ETA state."""
    return f"rohlikcz_{entry_id}_eta"


def announcements_of(data: dict | None) -> list:
    """Return the delivery announcements in coordinator data, or [].

    The API client stores None when the announcements request fails, so every
    level is read defensively.
    """
    return (
        (((data or {}).get("delivery_announcements") or {}).get("data") or {}).get(
            "announcements"
        )
        or []
    )


def announcement_slot(
    announcement: dict, upcoming_orders: list | None
) -> tuple[datetime | None, datetime | None]:
    """Return the booked slot (since, till) of the order an announcement is about.

    Only the upcoming order with the announcement's ID counts. The parser places
    the announced clock time on the slot's day, so another order's slot would
    move the ETA to that order's day. With no match it returns (None, None), and
    the parser keeps placing the time relative to when the announcement arrived.
    """
    announcement_id = str(announcement.get("id", ""))
    order = next(
        (o for o in upcoming_orders or [] if str(o.get("id", "")) == announcement_id),
        None,
    )
    if order is None:
        return None, None
    delivery_slot = order.get("deliverySlot") or {}
    return (
        parse_delivery_datetime_string(delivery_slot.get("since")),
        parse_delivery_datetime_string(delivery_slot.get("till")),
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return dt_util.parse_datetime(value)
    except (TypeError, ValueError):
        return None


class DeliveryEtaTracker:
    """Resolve and remember the ETA of the soonest upcoming order."""

    def __init__(self, hass: HomeAssistant, storage_key: str | None) -> None:
        self._store: Store[dict] | None = (
            Store(hass, ETA_STORE_VERSION, storage_key) if storage_key else None
        )
        # Remembered across refreshes (and restarts, via the store).
        self.last_value: datetime | None = None
        self.last_live_value: datetime | None = None
        self.last_live_order_id: str | None = None
        self.last_live_slot_since: datetime | None = None
        self.announcement_arrivals: dict[tuple[str, str], datetime] = {}
        #: True once state was loaded from the store (or seeded from legacy data).
        self.restored = False
        # Result of the last resolve().
        self._eta: datetime | None = None
        self._source: EtaSource | None = None
        self._order_id: str | None = None

    # ------------------------------------------------------------------ results

    @property
    def eta(self) -> datetime | None:
        """ETA of the soonest upcoming order, or None."""
        return self._eta

    @property
    def source(self) -> EtaSource | None:
        """Where the ETA came from."""
        return self._source

    @property
    def is_live(self) -> bool:
        """Whether the ETA comes from a courier announcement (current or kept)."""
        return self._source == "announcement"

    @property
    def order_id(self) -> str | None:
        """ID of the soonest upcoming order, or None when there is none."""
        return self._order_id

    @property
    def stale_at(self) -> datetime | None:
        """When the kept live ETA expires, so resolve() must run again."""
        if self.is_live and self.last_live_value is not None:
            # In UTC: wall-clock arithmetic on a local time ignores DST.
            return dt_util.as_utc(self.last_live_value) + LIVE_ETA_STALE_AFTER
        return None

    def as_diagnostics(self) -> dict[str, Any]:
        """Return the tracker state for diagnostics."""
        return {
            "eta": _iso(self._eta),
            "source": self._source,
            "order_id": self._order_id,
            "last_live_value": _iso(self.last_live_value),
            "last_live_order_id": self.last_live_order_id,
            "restored": self.restored,
        }

    # -------------------------------------------------------------- persistence

    async def async_load(self) -> None:
        """Load the remembered state from storage."""
        if self._store is None:
            return
        data = await self._store.async_load()
        if not data:
            return
        self.last_value = _parse(data.get("last_value"))
        self.last_live_value = _parse(data.get("last_live_value"))
        self.last_live_order_id = data.get("last_live_order_id")
        self.last_live_slot_since = _parse(data.get("last_live_slot_since"))
        arrivals: dict[tuple[str, str], datetime] = {}
        for item in data.get("announcement_arrivals") or []:
            try:
                order_id, content, received = item
            except (TypeError, ValueError):
                continue
            if (received_at := _parse(received)) is not None:
                arrivals[(str(order_id), str(content))] = received_at
        self.announcement_arrivals = arrivals
        self.restored = True

    def seed_legacy(
        self,
        last_value: datetime | None,
        last_live_value: datetime | None,
        last_live_order_id: str | None,
        last_live_slot_since: datetime | None,
    ) -> bool:
        """Adopt state restored by the Delivery Time entity (pre-store versions).

        Used only once, when no stored tracker state exists yet. Returns True
        when legacy state was adopted and the ETA should be resolved again.
        """
        if self.restored:
            return False
        self.restored = True
        self.async_schedule_save()
        if last_value is None and last_live_value is None:
            return False
        if self.last_value is None:
            self.last_value = last_value
        if self.last_live_value is None and last_live_value is not None:
            self.last_live_value = last_live_value
            self.last_live_order_id = last_live_order_id
            self.last_live_slot_since = last_live_slot_since
        return True

    def _data_to_save(self) -> dict:
        return {
            "last_value": _iso(self.last_value),
            "last_live_value": _iso(self.last_live_value),
            "last_live_order_id": self.last_live_order_id,
            "last_live_slot_since": _iso(self.last_live_slot_since),
            "announcement_arrivals": [
                [order_id, content, received.isoformat()]
                for (order_id, content), received in self.announcement_arrivals.items()
            ],
        }

    def async_schedule_save(self) -> None:
        """Write the state to disk soon."""
        if self._store is not None:
            self._store.async_delay_save(self._data_to_save, _SAVE_DELAY)

    async def async_close(self) -> None:
        """Write the state to disk now and stop saving (used on unload).

        A late update on an unloaded coordinator must not overwrite the file
        the reloaded entry is already using.
        """
        if self._store is not None:
            store, self._store = self._store, None
            await store.async_save(self._data_to_save())

    # ------------------------------------------------------------------ resolve

    def _clear_last_live_value(self) -> None:
        self.last_live_value = None
        self.last_live_order_id = None
        self.last_live_slot_since = None

    def _preserved_live_value(
        self,
        earliest_order_id: str | None,
        slot_since: datetime | None,
        now: datetime,
    ) -> datetime | None:
        """Return the kept live ETA when it still belongs to this order."""
        if (
            earliest_order_id is None
            or self.last_live_order_id != earliest_order_id
            or self.last_live_value is None
        ):
            return None

        if slot_since != self.last_live_slot_since:
            self._clear_last_live_value()
            return None

        # Compare instants: wall-clock arithmetic ignores a DST change.
        if now.timestamp() > (self.last_live_value + LIVE_ETA_STALE_AFTER).timestamp():
            self._clear_last_live_value()
            return None

        return self.last_live_value

    def resolve(
        self,
        data: dict | None,
        received_at: Callable[[dict], datetime],
        tz: ZoneInfo,
    ) -> None:
        """Recompute the ETA from coordinator data.

        With multiple concurrent orders the announcement may relate to a later
        order rather than the soonest one; then the soonest order's slot is
        used. When the announcement is cleared, its last live ETA is kept only
        while the same order remains the soonest upcoming order.
        """
        before = (self.last_value, self.last_live_value, self.last_live_order_id)
        self._eta, self._source = self._resolve(data or {}, received_at, tz)
        if (self.last_value, self.last_live_value, self.last_live_order_id) != before:
            self.async_schedule_save()

    def _resolve(
        self,
        data: dict,
        received_at: Callable[[dict], datetime],
        tz: ZoneInfo,
    ) -> tuple[datetime | None, EtaSource | None]:
        now = dt_util.now()
        announcements = announcements_of(data)
        upcoming_orders: list = data.get("next_order") or []
        earliest_order = get_earliest_order(upcoming_orders)
        earliest_order_id = (
            str(earliest_order.get("id"))
            if earliest_order is not None and earliest_order.get("id") is not None
            else None
        )
        delivery_slot = (earliest_order or {}).get("deliverySlot") or {}
        slot_since = parse_delivery_datetime_string(delivery_slot.get("since"))
        self._order_id = earliest_order_id

        if (
            self.last_live_order_id is not None
            and self.last_live_order_id != earliest_order_id
        ):
            self._clear_last_live_value()

        preserved_live_value = self._preserved_live_value(
            earliest_order_id, slot_since, now
        )

        if announcements:
            announcement = announcements[0]
            # Only trust the announcement when there is a single upcoming order
            # (no ambiguity) or when it explicitly matches the soonest order.
            announcement_matches_soonest = (
                len(upcoming_orders) <= 1
                or earliest_order is None
                or str(announcement.get("id", "")) == str(earliest_order.get("id", ""))
            )
            if announcement_matches_soonest:
                announced_since, announced_till = announcement_slot(
                    announcement, upcoming_orders
                )
                delivery_time = extract_delivery_datetime(
                    announcement.get("content", ""),
                    received_at(announcement),
                    tz=tz,
                    slot_since=announced_since,
                    slot_till=announced_till,
                )
                if delivery_time is not None:
                    self.last_value = delivery_time
                    self.last_live_value = delivery_time
                    self.last_live_order_id = earliest_order_id
                    self.last_live_slot_since = slot_since
                    return delivery_time, "announcement"

        if preserved_live_value is not None:
            # Rohlík commonly clears the announcement shortly before delivery,
            # or sends one without a time, or one about a later concurrent
            # order. Keep the precise ETA while the same order is the soonest.
            self.last_value = preserved_live_value
            return preserved_live_value, "announcement"

        # Fall back to the delivery slot of the soonest order.
        if slot_since is not None:
            self.last_value = slot_since
            return slot_since, "slot"

        # No live data available - keep the last known value while an order
        # still exists so it isn't cleared shortly before delivery.
        if upcoming_orders and self.last_value is not None:
            return self.last_value, "last_known"

        if not upcoming_orders:
            self._clear_last_live_value()
        return None, None


async def async_remove_eta_store(hass: HomeAssistant, storage_key: str) -> None:
    """Delete a config entry's stored ETA state."""
    await Store(hass, ETA_STORE_VERSION, storage_key).async_remove()
