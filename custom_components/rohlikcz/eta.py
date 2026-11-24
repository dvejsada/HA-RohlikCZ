"""Delivery ETA of the soonest upcoming order, shared by the delivery entities.

Owned by the coordinator so Delivery Time, Delivery Time Remaining, Delivery
Last Mile and the automatic ETA refresh all use one value, and persisted so a
restart does not lose a courier ETA after Rohlík clears the announcement.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from rohlik_api import DeliveryTracking, NextDelivery, UpcomingOrder

from .utils import extract_delivery_datetime, parse_delivery_datetime_string

#: An ETA is given up this long after it has passed: a kept courier ETA is
#: dropped, and Delivery Time Remaining / Delivery Last Mile stop.
MAX_OVERDUE = timedelta(hours=1)

#: Storage schema version for the ETA tracker.
ETA_STORE_VERSION = 1

#: Delay before writing tracker state to disk (coalesces frequent updates).
_SAVE_DELAY = 10

#: Where the current ETA came from.
EtaSource = Literal["tracking", "announcement", "slot", "last_known"]

#: Sources that are a courier's estimate rather than the booked slot.
LIVE_SOURCES: tuple[EtaSource, ...] = ("tracking", "announcement")


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


def soonest_upcoming(data: dict | None) -> tuple[UpcomingOrder | None, dict | None]:
    """Return the soonest upcoming order, typed and raw, or (None, None).

    The one place that decides which order is "the next delivery", shared by
    the hub's tracking and the ETA tracker so both always agree. Orders are
    sorted by slot start in absolute time; orders without a slot come last.
    """
    raw_orders = (data or {}).get("next_order")
    orders = UpcomingOrder.list_from_api(raw_orders)
    if not orders:
        return None, None
    soonest = orders[0]
    raw = next(
        (o for o in raw_orders if isinstance(o, dict) and o.get("id") == soonest.id),
        None,
    )
    return soonest, raw


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

    def __init__(self, hass: HomeAssistant, storage_key: str | None, tz: ZoneInfo) -> None:
        self._store: Store[dict] | None = (
            Store(hass, ETA_STORE_VERSION, storage_key) if storage_key else None
        )
        self._tz = tz
        # Remembered across refreshes (and restarts, via the store).
        self.last_value: datetime | None = None
        self.last_live_value: datetime | None = None
        self.last_live_order_id: str | None = None
        self.last_live_slot_since: datetime | None = None
        self.last_live_source: EtaSource = "announcement"
        # When each announcement (order ID, text) was first received.
        self.announcement_arrivals: dict[tuple[str, str], datetime] = {}
        #: True once state was loaded from the store (or migrated).
        self.restored = False
        # Result of the last update(); eta is in UTC.
        self.eta: datetime | None = None
        self.source: EtaSource | None = None
        self.order_id: str | None = None

    @property
    def is_live(self) -> bool:
        """Whether the ETA is a courier estimate (tracking or announcement, current or kept)."""
        return self.source in LIVE_SOURCES

    @property
    def stale_at(self) -> datetime | None:
        """When the kept courier ETA expires (UTC), so update() must run again."""
        return self._live_expires_at() if self.is_live else None

    def _live_expires_at(self) -> datetime | None:
        if self.last_live_value is None:
            return None
        return dt_util.as_utc(self.last_live_value) + MAX_OVERDUE

    def window(self, before: timedelta, after: timedelta) -> tuple[datetime, datetime] | None:
        """Return (ETA - before, ETA + after) in UTC, or None without an order."""
        if self.eta is None or self.order_id is None:
            return None
        return self.eta - before, self.eta + after

    def as_diagnostics(self) -> dict[str, Any]:
        """Return the tracker state for diagnostics."""
        return {
            "eta": _iso(self.eta),
            "source": self.source,
            "order_id": self.order_id,
            "last_live_value": _iso(self.last_live_value),
            "last_live_order_id": self.last_live_order_id,
            "last_live_source": self.last_live_source,
            "restored": self.restored,
        }

    # -------------------------------------------------------------- persistence

    async def async_load(self) -> None:
        """Load the remembered state from storage."""
        if self._store is None or not (data := await self._store.async_load()):
            return
        self.last_value = _parse(data.get("last_value"))
        self.last_live_value = _parse(data.get("last_live_value"))
        self.last_live_order_id = data.get("last_live_order_id")
        self.last_live_slot_since = _parse(data.get("last_live_slot_since"))
        # Stores written before tracking existed only kept announcement ETAs.
        source = data.get("last_live_source")
        self.last_live_source = source if source in LIVE_SOURCES else "announcement"
        self.announcement_arrivals = {
            (str(order_id), str(content)): received_at
            for order_id, content, received in data.get("announcement_arrivals") or []
            if (received_at := _parse(received)) is not None
        }
        self.restored = True

    def seed_legacy(
        self,
        last_value: datetime | None,
        last_live_value: datetime | None,
        last_live_order_id: str | None,
        last_live_slot_since: datetime | None,
    ) -> bool:
        """Adopt state restored by the Delivery Time entity of older versions.

        Runs once, when nothing was stored yet. Returns True when there was
        state to adopt, so the ETA should be resolved again.
        """
        if self.restored:
            return False
        self.restored = True
        self.async_schedule_save()
        if last_value is None and last_live_value is None:
            return False
        self.last_value = last_value
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
            "last_live_source": self.last_live_source,
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
        """Write the state now and stop saving, so a late update on an unloaded
        coordinator cannot overwrite the file the reloaded entry uses."""
        if self._store is not None:
            store, self._store = self._store, None
            await store.async_save(self._data_to_save())

    # ------------------------------------------------------------------ resolve

    @staticmethod
    def _announcement_key(announcement: dict) -> tuple[str, str]:
        return str(announcement.get("id")), announcement.get("content") or ""

    def received_at(self, announcement: dict) -> datetime:
        """Return when an announcement's current text was first received.

        A relative announcement ("za 2 minuty") counts from then: parsing the
        same text against the current time on every refresh would push the
        ETA later with each poll.
        """
        return self.announcement_arrivals.get(self._announcement_key(announcement)) or dt_util.now()

    def update(self, data: dict | None, tracking: DeliveryTracking | None = None) -> None:
        """Record new announcements and recompute the ETA from coordinator data.

        ``tracking`` is the hub's current (valid) delivery tracking, if any; it
        takes priority over announcements when it belongs to the soonest order
        and has a courier estimate.

        Arrivals of announcements no longer present are dropped, so the same
        text arriving again later starts a fresh clock.
        """
        data = data or {}
        before = (
            self.last_value, self.last_live_value, self.last_live_order_id,
            self.last_live_source, self.announcement_arrivals,
        )
        now = dt_util.now()
        self.announcement_arrivals = {
            key: self.announcement_arrivals.get(key, now)
            for key in map(self._announcement_key, announcements_of(data))
        }
        eta, self.source = self._resolve(data, tracking)
        self.eta = dt_util.as_utc(eta) if eta is not None else None
        after = (
            self.last_value, self.last_live_value, self.last_live_order_id,
            self.last_live_source, self.announcement_arrivals,
        )
        if after != before:
            self.async_schedule_save()

    def _clear_last_live_value(self) -> None:
        self.last_live_value = None
        self.last_live_order_id = None
        self.last_live_slot_since = None
        self.last_live_source = "announcement"

    def _remember_live(
        self, value: datetime, order_id: str | None, slot_since: datetime | None,
        source: EtaSource,
    ) -> None:
        self.last_value = value
        self.last_live_value = value
        self.last_live_order_id = order_id
        self.last_live_slot_since = slot_since
        self.last_live_source = source

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

        if slot_since != self.last_live_slot_since or dt_util.as_utc(now) > self._live_expires_at():
            self._clear_last_live_value()
            return None

        return self.last_live_value

    def _resolve(
        self, data: dict, tracking: DeliveryTracking | None
    ) -> tuple[datetime | None, EtaSource | None]:
        now = dt_util.now()
        announcements = announcements_of(data)
        upcoming_orders: list = data.get("next_order") or []
        soonest, earliest_order = soonest_upcoming(data)
        # Order IDs are compared as strings here: that is how they are stored.
        earliest_order_id = str(soonest.id) if soonest is not None else None
        delivery_slot = (earliest_order or {}).get("deliverySlot") or {}
        slot_since = parse_delivery_datetime_string(delivery_slot.get("since"))
        self.order_id = earliest_order_id

        if (
            self.last_live_order_id is not None
            and self.last_live_order_id != earliest_order_id
        ):
            self._clear_last_live_value()

        preserved_live_value = self._preserved_live_value(
            earliest_order_id, slot_since, now
        )

        # 1. The courier's tracked estimate for the soonest order.
        if (
            tracking is not None
            and soonest is not None
            and tracking.order_id == soonest.id
            and tracking.estimated_arrival is not None
            and not tracking.is_delivered
        ):
            try:
                resolved = NextDelivery.resolve(soonest, tracking)
            except ValueError:
                resolved = None
            if resolved is not None and resolved.eta is not None and resolved.eta_source == "tracking":
                delivery_time = dt_util.as_local(resolved.eta)
                self._remember_live(delivery_time, earliest_order_id, slot_since, "tracking")
                return delivery_time, "tracking"

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
                    self.received_at(announcement),
                    tz=self._tz,
                    slot_since=announced_since,
                    slot_till=announced_till,
                )
                if delivery_time is not None:
                    self._remember_live(
                        delivery_time, earliest_order_id, slot_since, "announcement"
                    )
                    return delivery_time, "announcement"

        if preserved_live_value is not None:
            # Rohlík commonly clears the announcement shortly before delivery,
            # or sends one without a time, or one about a later concurrent
            # order; tracking can fail for a while. Keep the precise ETA while
            # the same order is the soonest.
            self.last_value = preserved_live_value
            return preserved_live_value, self.last_live_source

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

