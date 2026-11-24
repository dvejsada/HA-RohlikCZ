from __future__ import annotations
import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_call_later, async_track_point_in_time
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from rohlik_api import (
    DeliveryTracking,
    InvalidCredentialsError,
    RohlikAPI,
    RohlikAPIError,
    Site,
    TrackingStatus,
    UpcomingOrder,
)

from .const import (
    DOMAIN, get_site,
    DEFAULT_AUTO_REFRESH_ETA, DEFAULT_ETA_REFRESH_INTERVAL, DEFAULT_LAST_MILE_LEAD,
)
from .eta import MAX_OVERDUE, DeliveryEtaTracker, eta_storage_key, soonest_upcoming

#: How often the integration refreshes data from the Rohlik API.
UPDATE_INTERVAL = timedelta(seconds=600)

#: Automatic ETA refresh runs from this long before the ETA ...
ETA_POLL_BEFORE = timedelta(minutes=30)
#: ... until this long after it.
ETA_POLL_AFTER = timedelta(minutes=10)

#: Delivery tracking is fetched once the soonest slot starts within this long.
TRACKING_LOOKAHEAD = timedelta(hours=4)
#: Kept tracking without a successful fetch for this long counts as unknown.
TRACKING_MAX_AGE = timedelta(minutes=15)
#: A delivered order's tracking is shown for this long after delivery.
DELIVERED_HOLD = timedelta(minutes=30)
#: Tracking of an order that left the upcoming list is dropped after this long.
LEFT_LIST_EXPIRY = timedelta(minutes=30)
#: Fetches for an order that left the upcoming list, to catch DELIVERED.
FINAL_FETCH_ATTEMPTS = 3
#: Longest automatic refresh interval once the courier is close.
LATE_STAGE_INTERVAL = timedelta(seconds=60)
#: Statuses in which the courier is on the way or at the door.
COURIER_COMING = (TrackingStatus.ON_THE_WAY, TrackingStatus.ARRIVED, TrackingStatus.HANDING_OVER)


@dataclass
class TrackedOrder:
    """Delivery tracking of one order, as last fetched by the hub."""

    order_id: int
    tracking: DeliveryTracking
    fetched_at: datetime
    not_upcoming_since: datetime | None = None
    delivered_at: datetime | None = None
    final_fetches: int = 0

    def is_fresh(self, now: datetime) -> bool:
        """Whether the last successful fetch is recent enough to trust."""
        return now - self.fetched_at <= TRACKING_MAX_AGE

    def is_expired(self, now: datetime) -> bool:
        """Whether this tracking should be dropped altogether."""
        if self.delivered_at is not None and now - self.delivered_at > DELIVERED_HOLD:
            return True
        return (
            self.not_upcoming_since is not None
            and now - self.not_upcoming_since > LEFT_LIST_EXPIRY
        )

    def change_key(self) -> tuple:
        """What entities care about; seconds-level ETA jitter is ignored."""
        t = self.tracking
        eta = t.estimated_arrival
        return (
            self.order_id,
            t.status,
            eta.replace(second=0, microsecond=0) if eta else None,
            t.stops_before,
            t.early_delivery_requested,
        )

_LOGGER = logging.getLogger(__name__)

_NOTIFICATIONS = {
    "en": {
        "title_progress": "Rohlik: Enrichment in progress",
        "title_complete": "Rohlik: Enrichment complete",
        "phase1": "Phase 1/2: Fetching item details for {count} orders...",
        "phase2": "Phase 2/2: Categorizing {count} products...",
        "phase2_progress": "Phase 2/2: Categorizing products: {done}/{total} ({pct}%)",
        "complete": (
            "Enrichment complete!\n"
            "Orders enriched: {orders_enriched}\n"
            "Products categorized: {products_categorized}\n"
            "Total orders: {total_orders} (enriched: {enriched_orders})\n"
            "Products in cache: {products_in_cache}"
        ),
    },
    "cs": {
        "title_progress": "Rohlik: Probíhá obohacování dat",
        "title_complete": "Rohlik: Obohacování dokončeno",
        "phase1": "Fáze 1/2: Stahování položek pro {count} objednávek...",
        "phase2": "Fáze 2/2: Kategorizace {count} produktů...",
        "phase2_progress": "Fáze 2/2: Kategorizace produktů: {done}/{total} ({pct} %)",
        "complete": (
            "Obohacování dokončeno!\n"
            "Obohacené objednávky: {orders_enriched}\n"
            "Kategorizované produkty: {products_categorized}\n"
            "Celkem objednávek: {total_orders} (obohacených: {enriched_orders})\n"
            "Produktů v mezipaměti: {products_in_cache}"
        ),
    },
}


#: Storage schema version for the order store.
STORE_VERSION = 3


class OrderStore:
    """Persistent storage for order history backed by Home Assistant's Store helper."""

    def __init__(self, hass: HomeAssistant, user_id: str):
        self._hass = hass
        self._user_id = user_id
        self._key = f"rohlikcz_{user_id}_orders"
        self._store: Store[dict] = Store(hass, STORE_VERSION, self._key)
        # Path of the pre-Store implementation, imported once if present.
        self._legacy_path = hass.config.path(".storage", f"{self._key}.json")
        self._data = self._default_data(user_id)

    @staticmethod
    def _default_data(user_id: str) -> dict:
        return {"version": STORE_VERSION, "user_id": user_id, "tracking_since": None,
                "backfill_complete": False, "orders": {}, "product_categories": {}}

    @classmethod
    async def async_create(cls, hass: HomeAssistant, user_id: str) -> "OrderStore":
        """Create and load an OrderStore asynchronously."""
        store = cls(hass, user_id)
        await store._async_load()
        return store

    async def _async_load(self) -> None:
        """Load from Store, importing the legacy JSON file on first run."""
        data = await self._store.async_load()
        if data is not None:
            self._data = data
            return

        # No Store data yet: import the legacy file if one exists.
        legacy = await self._hass.async_add_executor_job(self._load_legacy_sync)
        if legacy is not None:
            self._data = self._migrate_legacy(legacy, self._user_id)
            await self._store.async_save(self._data)
            await self._hass.async_add_executor_job(self._remove_legacy_sync)
            _LOGGER.info("Imported legacy order store for user %s into HA storage", self._user_id)

    def _load_legacy_sync(self) -> dict | None:
        """Read the legacy JSON file (blocking, run in executor)."""
        if not os.path.exists(self._legacy_path):
            return None
        try:
            with open(self._legacy_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as err:
            _LOGGER.error("Failed to load legacy order store: %s", err)
            return None

    def _remove_legacy_sync(self) -> None:
        """Delete the legacy JSON file after a successful import."""
        try:
            os.remove(self._legacy_path)
        except OSError as err:
            _LOGGER.warning("Could not remove legacy order store file %s: %s", self._legacy_path, err)

    @staticmethod
    def _migrate_legacy(data: dict, user_id: str = "") -> dict:
        """Bring imported legacy data up to the current schema (v1/v2 -> v3)."""
        if not isinstance(data, dict):
            return OrderStore._default_data(user_id)
        data = dict(data)  # avoid mutating the caller's dict
        data.setdefault("orders", {})
        data.setdefault("product_categories", {})
        if "backfill_complete" not in data:
            # A store that already had a tracking_since had completed backfill.
            data["backfill_complete"] = data.get("tracking_since") is not None
        data["version"] = STORE_VERSION
        return data

    async def async_save(self) -> None:
        """Persist the order store asynchronously."""
        await self._store.async_save(self._data)

    def process_orders(self, orders: list) -> int:
        """Process a list of order dicts from the API. Returns count of new orders added.

        Note: Caller must call await async_save() after this if new_count > 0.
        """
        if not orders:
            return 0

        new_count = 0
        for order in orders:
            order_id = str(order.get("id", ""))
            if not order_id or order_id in self._data["orders"]:
                continue

            price_comp = order.get("priceComposition", {})
            if not isinstance(price_comp, dict):
                continue

            total = price_comp.get("total", {})
            if not isinstance(total, dict):
                continue
            amount = total.get("amount")
            if amount is None:
                continue

            try:
                amount = float(amount)
            except (ValueError, TypeError):
                continue

            order_time = order.get("orderTime", "")
            date_str = order_time[:10] if order_time else ""

            self._data["orders"][order_id] = {"date": date_str, "amount": amount}
            new_count += 1

        if new_count > 0:
            if not self._data["tracking_since"]:
                self._data["tracking_since"] = dt_util.now().isoformat()
            _LOGGER.info(f"Added {new_count} new orders to store. Total: {len(self._data['orders'])}")

        return new_count

    @property
    def orders(self) -> dict:
        return self._data["orders"]

    @property
    def tracking_since(self) -> str | None:
        return self._data.get("tracking_since")

    @property
    def backfill_complete(self) -> bool:
        """Whether the initial full order history download has finished."""
        return self._data.get("backfill_complete", False)

    def mark_backfill_complete(self) -> None:
        """Mark the initial backfill as done (caller must async_save)."""
        self._data["backfill_complete"] = True

    def yearly_total(self, year: str) -> float:
        """Sum of order amounts for a given year (e.g. '2026')."""
        return sum(
            o["amount"] for o in self._data["orders"].values()
            if o["date"].startswith(year)
        )

    def yearly_count(self, year: str) -> int:
        """Count of orders for a given year."""
        return sum(1 for o in self._data["orders"].values() if o["date"].startswith(year))

    def yearly_enriched_count(self, year: str) -> int:
        """Count of enriched orders (with item details) for a given year."""
        return sum(1 for o in self._data["orders"].values() if o["date"].startswith(year) and "items" in o)

    def yearly_breakdown(self) -> dict[str, dict]:
        """Per-year totals and counts for every tracked year.

        Returns a mapping like {"2025": {"total": 1234.5, "order_count": 12}, ...}
        sorted by year descending (most recent first).
        """
        breakdown: dict[str, dict] = {}
        for order in self._data["orders"].values():
            date = order.get("date") or ""
            year = date[:4]
            if len(year) != 4:
                continue
            entry = breakdown.setdefault(year, {"total": 0.0, "order_count": 0})
            entry["total"] += order["amount"]
            entry["order_count"] += 1

        for entry in breakdown.values():
            entry["total"] = round(entry["total"], 2)

        return {year: breakdown[year] for year in sorted(breakdown, reverse=True)}

    def alltime_total(self) -> float:
        """Sum of all order amounts."""
        return sum(o["amount"] for o in self._data["orders"].values())

    def alltime_count(self) -> int:
        """Count of all orders."""
        return len(self._data["orders"])

    def first_order_date(self) -> str | None:
        """Date of the earliest order."""
        dates = [o["date"] for o in self._data["orders"].values() if o["date"]]
        return min(dates) if dates else None

    def add_items_to_order(self, order_id: str, items: list) -> bool:
        """Add item details to an existing order. Returns True if added."""
        if order_id not in self._data["orders"]:
            return False
        if "items" in self._data["orders"][order_id]:
            return False  # Already has items
        self._data["orders"][order_id]["items"] = items
        return True

    def update_product_categories(self, categories_map: dict[int, list]) -> int:
        """Update the product->category cache. Returns count of new entries.

        Note: Caller must call await async_save() after this if new_count > 0.
        """
        new_count = 0
        for pid, cats in categories_map.items():
            pid_str = str(pid)
            if pid_str not in self._data["product_categories"]:
                # Store simplified: {pid: {l0: name, l1: name, l2: name, l3: name}}
                cat_dict = {}
                for cat in cats:
                    level = cat.get("level")
                    if level is not None:
                        cat_dict[f"l{level}"] = cat.get("name", "Unknown")
                self._data["product_categories"][pid_str] = cat_dict
                new_count += 1
        return new_count

    def get_product_category(self, product_id: int, level: int = 1) -> str:
        """Get category name for a product at a given level. Returns 'Uncategorized' if not found."""
        pid_str = str(product_id)
        cat_data = self._data.get("product_categories", {}).get(pid_str, {})
        return cat_data.get(f"l{level}", "Uncategorized")

    def _is_discontinued(self, product_id: int) -> bool:
        """Check if a product is discontinued (has sentinel 'Discontinued' category)."""
        pid_str = str(product_id)
        cat_data = self._data.get("product_categories", {}).get(pid_str, {})
        return cat_data.get("l1") == "Discontinued"

    @property
    def unenriched_order_ids(self) -> list[str]:
        """Order IDs that don't have item details yet."""
        return [oid for oid, o in self._data["orders"].items() if "items" not in o]

    @property
    def enriched_count(self) -> int:
        """Count of orders with item details."""
        return sum(1 for o in self._data["orders"].values() if "items" in o)

    @property
    def cached_product_count(self) -> int:
        """Count of products with cached categories."""
        return len(self._data.get("product_categories", {}))

    def uncategorized_product_ids(self) -> list[int]:
        """Product IDs found in enriched orders but not yet in category cache."""
        known = set(self._data.get("product_categories", {}).keys())
        found = set()
        for order in self._data["orders"].values():
            for item in order.get("items", []):
                pid = item.get("id")
                if pid is not None:
                    found.add(str(pid))
        return [int(pid) for pid in found - known]

    def category_totals(self, year: str | None = None, level: int = 1, hide_discontinued: bool = False) -> list[dict]:
        """Aggregate spending by category at given level. Optionally filter by year/month prefix.

        Returns sorted list: [{"name": "Dairy", "spent": 5000.0, "units": 200, "avg_unit_price": 25.0}]
        """
        from collections import defaultdict
        cats = defaultdict(lambda: {"spent": 0.0, "units": 0})

        for order in self._data["orders"].values():
            if year and not order["date"].startswith(year):
                continue
            for item in order.get("items", []):
                pid = item.get("id")
                if hide_discontinued and pid and self._is_discontinued(pid):
                    continue
                cat_name = self.get_product_category(pid, level) if pid else "Uncategorized"
                cats[cat_name]["spent"] += item.get("price", 0)
                cats[cat_name]["units"] += item.get("quantity", 1)

        if hide_discontinued:
            cats.pop("Discontinued", None)

        result = []
        for name, vals in cats.items():
            avg = round(vals["spent"] / vals["units"], 2) if vals["units"] > 0 else 0.0
            result.append({
                "name": name,
                "spent": round(vals["spent"], 2),
                "units": vals["units"],
                "avg_unit_price": avg,
            })
        result.sort(key=lambda x: x["spent"], reverse=True)
        return result

    def item_totals(self, year: str | None = None, hide_discontinued: bool = False) -> list[dict]:
        """Aggregate spending by individual product. Optionally filter by year.

        Returns sorted list: [{"name": "Tchibo Barista", "id": 1328327, "spent": 500.0, "units": 10, "avg_unit_price": 50.0}]
        """
        from collections import defaultdict
        items = defaultdict(lambda: {"name": "", "spent": 0.0, "units": 0})

        for order in self._data["orders"].values():
            if year and not order["date"].startswith(year):
                continue
            for item in order.get("items", []):
                pid = item.get("id")
                if not pid:
                    continue
                if hide_discontinued and self._is_discontinued(pid):
                    continue
                items[pid]["name"] = item.get("name", "Unknown")
                items[pid]["spent"] += item.get("price", 0)
                items[pid]["units"] += item.get("quantity", 1)

        result = []
        for pid, vals in items.items():
            avg = round(vals["spent"] / vals["units"], 2) if vals["units"] > 0 else 0.0
            result.append({
                "name": vals["name"],
                "id": pid,
                "spent": round(vals["spent"], 2),
                "units": vals["units"],
                "avg_unit_price": avg,
            })
        result.sort(key=lambda x: x["spent"], reverse=True)
        return result


class RohlikAccount(DataUpdateCoordinator[dict]):
    """RohlikCZ account modelled as a Home Assistant data update coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        username: str,
        password: str,
        analytics: list[str] | None = None,
        top_n: int = 10,
        hide_discontinued: bool = True,
        entry: ConfigEntry | None = None,
        site: str | None = None,
        auto_refresh_eta: bool = DEFAULT_AUTO_REFRESH_ETA,
        eta_refresh_interval: int = DEFAULT_ETA_REFRESH_INTERVAL,
        last_mile_lead: int = DEFAULT_LAST_MILE_LEAD,
    ) -> None:
        """Initialize account info."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        self._username: str = username
        self._password: str = password
        self._site: Site = get_site(site)
        # get_time_zone is cached; async_setup_entry preloads the zone off the
        # event loop, so this does no file I/O there.
        self._timezone: ZoneInfo = dt_util.get_time_zone(self._site.timezone)
        # A dedicated, HA-managed aiohttp session (own cookie jar) keeps each
        # account's auth cookies isolated from other integrations and from a
        # second Rohlik account. The client logs in lazily and re-authenticates
        # transparently on a 401, reusing this session across calls.
        self._session = async_create_clientsession(hass)
        self._client = RohlikAPI(
            self._username, self._password, base_url=self._site.base_url, session=self._session
        )
        self._order_store: OrderStore | None = None
        self._last_refresh: datetime | None = None
        # The soonest order's delivery ETA, shared by the delivery-time entities.
        self._eta = DeliveryEtaTracker(
            hass,
            eta_storage_key(entry.entry_id) if entry is not None else None,
            self._timezone,
        )
        self._eta_stale_unsub: CALLBACK_TYPE | None = None
        self._eta_poll_unsub: CALLBACK_TYPE | None = None
        # Live delivery tracking (kept outside self.data, which stays raw JSON).
        self._tracked: TrackedOrder | None = None
        self._no_tracking: set[int] = set()
        self._closed = False
        self._auto_refresh_eta = auto_refresh_eta
        self._eta_refresh_interval = timedelta(seconds=eta_refresh_interval)
        self._last_mile_lead = timedelta(minutes=last_mile_lead)
        # _store_lock guards brief in-memory store mutations (contended by the
        # refresh cycle). _enrich_lock serializes whole enrichment runs and may
        # be held across network I/O without blocking the refresh.
        self._store_lock = asyncio.Lock()
        self._enrich_lock = asyncio.Lock()
        self._analytics: list[str] = analytics or []
        self._top_n: int = top_n
        self._hide_discontinued: bool = hide_discontinued

    @property
    def analytics_enabled(self) -> bool:
        """Whether any analytics level is selected."""
        return len(self._analytics) > 0

    @property
    def analytics(self) -> list[str]:
        """Selected analytics levels."""
        return self._analytics

    @property
    def top_n(self) -> int:
        """Number of top items to include in sensor attributes."""
        return self._top_n

    @property
    def hide_discontinued(self) -> bool:
        """Whether to exclude discontinued products from top N."""
        return self._hide_discontinued

    @property
    def eta(self) -> DeliveryEtaTracker:
        """Delivery ETA of the soonest upcoming order."""
        return self._eta

    @property
    def last_mile_lead(self) -> timedelta:
        """How long before the ETA the Last Mile sensor turns on."""
        return self._last_mile_lead

    @property
    def currency(self) -> str:
        """ISO currency code of the account's shop, e.g. ``CZK``."""
        return self._site.currency

    @property
    def timezone(self) -> ZoneInfo:
        """Local timezone of the account's shop."""
        return self._timezone

    @property
    def has_address(self) -> bool:
        return bool((self.data or {}).get("next_delivery_slot"))

    @property
    def device_info(self) -> DeviceInfo:
        """ Provides a device info. """
        return {"identifiers": {(DOMAIN, self.data["login"]["data"]["user"]["id"])}, "name": self.data["login"]["data"]["user"]["name"], "manufacturer": self._site.name}

    @property
    def account_name(self) -> str:
        """Provides display name for the account holder.

        Note: ``name`` is reserved by DataUpdateCoordinator, so the account's
        display name is exposed separately.
        """
        return self.data["login"]["data"]["user"]["name"]

    @property
    def unique_id(self) -> str:
        """Return the unique ID for this account."""
        return self.data["login"]["data"]["user"]["id"]

    @property
    def is_ordered(self) -> bool:
        return len(self.data.get('next_order') or []) > 0

    @property
    def order_store(self) -> OrderStore | None:
        return self._order_store

    @property
    def last_refresh(self) -> datetime | None:
        """Timestamp of the last data fetch from the API."""
        return self._last_refresh

    @callback
    def async_update_listeners(self) -> None:
        """Resolve the ETA, re-arm the timers that depend on it, notify entities."""
        # After unload, a late update (e.g. an in-flight service call) must
        # not touch the ETA state or arm timers.
        if not self._closed:
            self._eta.update(self.data, self.tracking)
            self._arm_eta_stale_timer()
            self._schedule_eta_poll()
        super().async_update_listeners()

    # ---------------------------------------------------------- delivery tracking

    @property
    def tracking(self) -> DeliveryTracking | None:
        """The tracked order's delivery tracking, if fresh and not expired."""
        tracked = self._tracked
        now = dt_util.utcnow()
        if tracked is None or not tracked.is_fresh(now) or tracked.is_expired(now):
            return None
        return tracked.tracking

    @property
    def tracked_order(self) -> TrackedOrder | None:
        """The raw tracked-order state (for diagnostics)."""
        return self._tracked

    @property
    def tracking_valid_until(self) -> datetime | None:
        """When the current tracking stops counting as valid (UTC), or None."""
        tracked = self._tracked
        if tracked is None or self.tracking is None:
            return None
        limits = [tracked.fetched_at + TRACKING_MAX_AGE]
        if tracked.delivered_at is not None:
            limits.append(tracked.delivered_at + DELIVERED_HOLD)
        if tracked.not_upcoming_since is not None:
            limits.append(tracked.not_upcoming_since + LEFT_LIST_EXPIRY)
        return min(limits)

    async def _fetch_tracking(self, order_id: int) -> tuple[str, DeliveryTracking | None]:
        """Fetch tracking: ("ok", t), ("not_found", None) or ("failed", None)."""
        try:
            tracking = await self._client.orders.get_delivery_tracking(
                order_id, raise_on_error=True
            )
        except (RohlikAPIError, ValueError, TypeError) as err:
            _LOGGER.debug("Delivery tracking for order %s failed: %s", order_id, err)
            return "failed", None
        if tracking is None:
            return "not_found", None
        return "ok", tracking

    def _store_tracking(self, order_id: int, tracking: DeliveryTracking, now: datetime) -> bool:
        """Store a successful fetch. Returns True when it just became DELIVERED."""
        tracked = self._tracked
        if tracked is None or tracked.order_id != order_id:
            tracked = self._tracked = TrackedOrder(order_id, tracking, now)
        else:
            tracked.tracking = tracking
            tracked.fetched_at = now
        if tracking.is_delivered and tracked.delivered_at is None:
            tracked.delivered_at = now
            return True
        return False

    async def _async_refresh_tracking(self, data: dict | None) -> tuple[bool, bool]:
        """Refresh delivery tracking from fresh coordinator ``data``.

        Returns (changed, just_delivered). Never raises: tracking is optional.
        """
        now = dt_util.utcnow()
        before = self._tracking_key(now)
        just_delivered = False
        raw_orders = (data or {}).get("next_order")
        if raw_orders is not None:  # None: the upcoming request failed, keep state
            upcoming = UpcomingOrder.list_from_api(raw_orders)
            upcoming_ids = {order.id for order in upcoming}
            soonest, _raw = soonest_upcoming(data)
            tracked = self._tracked

            # The tracked order left the upcoming list (delivered or cancelled).
            if tracked is not None:
                if tracked.order_id in upcoming_ids:
                    tracked.not_upcoming_since = None
                elif tracked.not_upcoming_since is None:
                    tracked.not_upcoming_since = now

            target: int | None = None
            if soonest is not None and soonest.id not in self._no_tracking and (
                soonest.slot_since is None or soonest.slot_since - now <= TRACKING_LOOKAHEAD
            ):
                target = soonest.id
            elif (
                tracked is not None
                and tracked.not_upcoming_since is not None
                and not tracked.tracking.is_delivered
                and tracked.final_fetches < FINAL_FETCH_ATTEMPTS
            ):
                tracked.final_fetches += 1
                target = tracked.order_id

            if target is not None:
                outcome, tracking = await self._fetch_tracking(target)
                if outcome == "ok" and tracking is not None:
                    just_delivered = self._store_tracking(target, tracking, now)
                elif outcome == "not_found":
                    self._no_tracking.add(target)
                    if self._tracked is not None and self._tracked.order_id == target:
                        self._tracked = None
                # "failed": keep what we had; it ages out via TRACKING_MAX_AGE.

        if self._tracked is not None and self._tracked.is_expired(now):
            self._tracked = None
        return self._tracking_key(now) != before, just_delivered

    def _tracking_key(self, now: datetime) -> tuple | None:
        tracked = self._tracked
        if tracked is None or not tracked.is_fresh(now) or tracked.is_expired(now):
            return None
        return tracked.change_key()

    def announcement_received_at(self, announcement: dict) -> datetime:
        """Return when a delivery announcement's current text was first received."""
        return self._eta.received_at(announcement)

    @callback
    def _arm_eta_stale_timer(self) -> None:
        """Notify entities when the kept courier ETA expires without new data."""
        if self._eta_stale_unsub is not None:
            self._eta_stale_unsub()
            self._eta_stale_unsub = None
        if (stale_at := self._eta.stale_at) is not None:
            self._eta_stale_unsub = async_track_point_in_time(
                self.hass, self._handle_eta_stale, stale_at + timedelta(seconds=1)
            )

    @callback
    def _handle_eta_stale(self, _now: datetime) -> None:
        self._eta_stale_unsub = None
        self.async_update_listeners()

    def _late_stage(self, now: datetime) -> bool:
        """Whether the courier is close: poll often, even past ETA + 10 min.

        True while tracking shows the courier on the way or at the door, or
        at most one stop before this order, until the courier estimate plus
        MAX_OVERDUE. Bounded: stale tracking (TRACKING_MAX_AGE) ends it.
        """
        tracking = self.tracking
        if tracking is None or tracking.is_delivered:
            return False
        stops = tracking.stops_before
        if tracking.status not in COURIER_COMING and (stops is None or stops > 1):
            return False
        estimate = tracking.estimated_arrival
        return estimate is None or now <= estimate + MAX_OVERDUE

    def _poll_window(self) -> tuple[datetime, datetime] | None:
        """ETA-30 min .. ETA+10 min around the courier's uncapped estimate.

        Using the uncapped estimate means a courier well ahead of the slot is
        still polled for, although the shown ETA is held at the slot start.
        """
        tracking = self.tracking
        if (
            tracking is not None
            and tracking.estimated_arrival is not None
            and not tracking.is_delivered
            and self._eta.order_id == str(tracking.order_id)
        ):
            estimate = tracking.estimated_arrival
            return estimate - ETA_POLL_BEFORE, estimate + ETA_POLL_AFTER
        return self._eta.window(ETA_POLL_BEFORE, ETA_POLL_AFTER)

    @callback
    def _schedule_eta_poll(self) -> None:
        """Arm the next automatic ETA refresh.

        Runs from ETA-30 min to ETA+10 min, and in the late stage (courier
        close, see _late_stage) at least every LATE_STAGE_INTERVAL.
        """
        if self._eta_poll_unsub is not None:
            self._eta_poll_unsub()
            self._eta_poll_unsub = None
        if not self._auto_refresh_eta or self._closed:
            return
        now = dt_util.utcnow()
        if self._late_stage(now):
            self._eta_poll_unsub = async_call_later(
                self.hass,
                min(self._eta_refresh_interval, LATE_STAGE_INTERVAL),
                self._handle_eta_poll,
            )
            return
        if (window := self._poll_window()) is None:
            return
        start, end = window
        if now < start:
            self._eta_poll_unsub = async_track_point_in_time(
                self.hass, self._handle_eta_poll, start
            )
        elif now <= end:
            self._eta_poll_unsub = async_call_later(
                self.hass, self._eta_refresh_interval, self._handle_eta_poll
            )

    @callback
    def _handle_eta_poll(self, _now: datetime) -> None:
        self._eta_poll_unsub = None
        self.config_entry.async_create_background_task(
            self.hass, self._async_eta_poll(), "rohlik_eta_refresh"
        )

    async def _async_eta_poll(self) -> None:
        """Refresh the delivery announcement, then arm the next refresh."""
        try:
            await self.refresh_delivery_times()
        except Exception as err:  # noqa: BLE001 - never let polling break
            _LOGGER.debug("Automatic delivery time refresh failed: %s", err)
        # Re-arm also when the request failed (it then notifies nobody).
        self._schedule_eta_poll()

    async def _async_update_data(self) -> dict:
        """Fetch data from the Rohlik API (called by the coordinator)."""
        try:
            data = await self._client.get_data()
        except InvalidCredentialsError as err:
            # Credentials are no longer valid - trigger the reauth flow.
            raise ConfigEntryAuthFailed(str(err)) from err
        except RohlikAPIError as err:
            raise UpdateFailed(str(err)) from err

        self._last_refresh = datetime.now(self._timezone)

        # Delivery tracking from the fresh data (self.data is still the
        # previous cycle's here, or None on the first refresh).
        if not self._closed:
            try:
                await self._async_refresh_tracking(data)
            except Exception as err:  # noqa: BLE001 - tracking is optional
                _LOGGER.debug("Delivery tracking refresh failed: %s", err)

        # Initialize order store on first update (only if analytics enabled)
        if self._analytics and not self._order_store and data.get("login"):
            user_id = str(data["login"]["data"]["user"]["id"])
            self._order_store = await OrderStore.async_create(self.hass, user_id)

        # Process delivered orders into persistent store and auto-enrich new ones
        if self._analytics and self._order_store and data.get("delivered_orders"):
            async with self._store_lock:
                new = self._order_store.process_orders(data["delivered_orders"])
                if new > 0:
                    await self._order_store.async_save()
            if new > 0:
                # Schedule enrichment in background (don't block update cycle / setup)
                self.config_entry.async_create_background_task(
                    self.hass, self._auto_enrich_new_orders(new), "rohlik_auto_enrich"
                )

        return data

    async def async_update(self) -> None:
        """Force an immediate data refresh (used by services and after cart changes)."""
        await self.async_refresh()

    async def _auto_enrich_new_orders(self, new_count: int) -> None:
        """Auto-enrich recently added orders in the background.

        The store lock is held only around in-memory reads/writes, never during
        network I/O, so a slow enrichment doesn't block the regular refresh
        cycle (which also takes the lock).
        """
        try:
            # Serialize against manual backfill/enrichment runs.
            async with self._enrich_lock:
                # Snapshot which orders still need enrichment.
                async with self._store_lock:
                    unenriched = list(self._order_store.unenriched_order_ids)
                    recent_unenriched = unenriched[-new_count:] if len(unenriched) >= new_count else unenriched
                if not recent_unenriched:
                    return

                # Fetch item details (network I/O, store lock released).
                items_map = await self._fetch_order_items(recent_unenriched)

                # Apply item results and find products needing categories.
                async with self._store_lock:
                    for order_id, items in items_map.items():
                        self._order_store.add_items_to_order(order_id, items)
                    uncategorized = self._order_store.uncategorized_product_ids()

                # Fetch categories (network I/O, store lock released).
                cat_map = {}
                if uncategorized:
                    cat_map = await self._fetch_product_categories(uncategorized)

                # Persist all results.
                enriched = False
                async with self._store_lock:
                    if cat_map:
                        self._order_store.update_product_categories(cat_map)
                    if items_map:
                        await self._order_store.async_save()
                        enriched = True

            if enriched:
                _LOGGER.info(f"Auto-enriched {len(items_map)} new orders")
                self.async_update_listeners()
        except Exception as err:
            _LOGGER.warning(f"Auto-enrichment of new orders failed: {err}")

    async def fetch_full_order_history(self, hass=None) -> dict:
        """Fetch all historical orders, enrich with items, and categorize products.

        Returns dict with stats.
        """
        async with self._enrich_lock:
            # Step 1: Fetch order list (network I/O, store lock released)
            all_orders = await self._client.orders.get_all_delivered()
            new_orders = 0
            if self._order_store and all_orders:
                async with self._store_lock:
                    new_orders = self._order_store.process_orders(all_orders)
                    if new_orders > 0:
                        await self._order_store.async_save()

            # Step 2: Enrich with items and categories (manages its own lock)
            if self._order_store:
                enrich_stats = await self._enrich_order_details(hass=hass)
                enrich_stats["new_orders"] = new_orders
                # Mark backfill as done so we don't re-download on next restart
                if not self._order_store.backfill_complete:
                    async with self._store_lock:
                        self._order_store.mark_backfill_complete()
                        await self._order_store.async_save()
                return enrich_stats

            return {"total_orders": 0, "new_orders": 0}

    def _t(self, key: str) -> str:
        """Get localized notification string based on HA language."""
        lang = self.hass.config.language or "en"
        return _NOTIFICATIONS.get(lang, _NOTIFICATIONS["en"]).get(key, _NOTIFICATIONS["en"][key])

    async def _notify(self, hass, message: str, title: str, notification_id: str = "rohlik_enrichment") -> None:
        """Create a persistent notification via service call."""
        if not hass:
            return
        await hass.services.async_call(
            "persistent_notification", "create",
            {"message": message, "title": title, "notification_id": notification_id},
        )

    async def enrich_order_details(self, hass=None) -> dict:
        """Fetch item details and categories for all unenriched orders.

        Public entry point — serializes enrichment runs via _enrich_lock.
        """
        async with self._enrich_lock:
            return await self._enrich_order_details(hass=hass)

    async def _enrich_order_details(self, hass=None) -> dict:
        """Internal enrichment logic — caller must hold _enrich_lock.

        Two-phase enrichment:
        1. Fetch items for orders missing them
        2. Fetch categories for products not yet in cache

        The store lock is taken only around in-memory mutations, never during
        the network calls, so the regular refresh cycle isn't blocked.

        Returns stats dict.
        """
        if not self._order_store:
            return {"error": "Order store not initialized"}

        stats = {"orders_enriched": 0, "products_categorized": 0, "errors": 0}

        # Phase 1: Fetch items for unenriched orders
        async with self._store_lock:
            unenriched = list(self._order_store.unenriched_order_ids)
        if unenriched:
            await self._notify(hass,
                self._t("phase1").format(count=len(unenriched)),
                self._t("title_progress"))
            _LOGGER.info(f"Enriching {len(unenriched)} orders with item details...")
            items_map = await self._fetch_order_items(unenriched)
            async with self._store_lock:
                for order_id, items in items_map.items():
                    if self._order_store.add_items_to_order(order_id, items):
                        stats["orders_enriched"] += 1
                if stats["orders_enriched"] > 0:
                    await self._order_store.async_save()
            if stats["orders_enriched"] > 0:
                _LOGGER.info(f"Added items to {stats['orders_enriched']} orders")

        # Phase 2: Fetch categories for uncategorized products
        async with self._store_lock:
            uncategorized = self._order_store.uncategorized_product_ids()
        if uncategorized:
            total_products = len(uncategorized)
            await self._notify(hass,
                self._t("phase2").format(count=total_products),
                self._t("title_progress"))

            async def progress_cb(done, total):
                pct = round(done / total * 100) if total > 0 else 0
                _LOGGER.info(f"Category enrichment progress: {done}/{total} ({pct}%)")
                await self._notify(hass,
                    self._t("phase2_progress").format(done=done, total=total, pct=pct),
                    self._t("title_progress"))

            _LOGGER.info(f"Fetching categories for {total_products} products...")
            cat_map = await self._fetch_product_categories(uncategorized, progress_callback=progress_cb)
            async with self._store_lock:
                new_cats = self._order_store.update_product_categories(cat_map)
                stats["products_categorized"] = new_cats
                if new_cats > 0:
                    await self._order_store.async_save()
            _LOGGER.info(f"Categorized {new_cats} products")

        # Done - only notify if something was actually enriched this run.
        if stats["orders_enriched"] > 0 or stats["products_categorized"] > 0:
            await self._notify(hass,
                self._t("complete").format(
                    orders_enriched=stats["orders_enriched"],
                    products_categorized=stats["products_categorized"],
                    total_orders=self._order_store.alltime_count(),
                    enriched_orders=self._order_store.enriched_count,
                    products_in_cache=self._order_store.cached_product_count,
                ),
                self._t("title_complete"))

        self.async_update_listeners()
        return {
            "total_orders": self._order_store.alltime_count(),
            "enriched_orders": self._order_store.enriched_count,
            "unenriched_remaining": len(self._order_store.unenriched_order_ids),
            "products_in_cache": self._order_store.cached_product_count,
            "orders_enriched_this_run": stats["orders_enriched"],
            "products_categorized_this_run": stats["products_categorized"],
        }

    async def _fetch_order_items(self, order_ids: list[str]) -> dict[str, list]:
        """Fetch line items for a list of order IDs.

        Returns ``{order_id: items_list}`` where each item is normalised to the
        shape the OrderStore expects. Orders are fetched one at a time with a
        short delay to stay polite to the API.
        """
        results: dict[str, list] = {}
        total = len(order_ids)
        for i, order_id in enumerate(order_ids):
            try:
                detail = await self._client.orders.get_detail(int(order_id))
            except (ValueError, RohlikAPIError) as err:
                _LOGGER.warning("Failed to fetch items for order %s: %s", order_id, err)
                detail = None
            if detail and detail.get("items"):
                results[order_id] = [
                    {
                        "id": item.get("id"),
                        "name": item.get("name", "Unknown"),
                        "quantity": item.get("amount", 1),
                        "price": item.get("priceComposition", {}).get("total", {}).get("amount", 0),
                        "unit_price": item.get("priceComposition", {}).get("unit", {}).get("amount", 0),
                        "textual_amount": item.get("textualAmount", ""),
                    }
                    for item in detail["items"]
                ]
            if i and i % 50 == 0:
                _LOGGER.info("Fetched items for %d/%d orders", i, total)
            await asyncio.sleep(0.2)
        _LOGGER.info("Item fetch complete: %d/%d orders", len(results), total)
        return results

    async def _fetch_product_categories(self, product_ids: list[int], progress_callback=None) -> dict[int, list]:
        """Fetch the category hierarchy for a batch of product IDs.

        Returns ``{product_id: categories_list}``. A product the API no longer
        knows about (``get_categories`` returns ``None``) is recorded with a
        sentinel "Discontinued" category so it isn't retried every run.
        """
        results: dict[int, list] = {}
        total = len(product_ids)
        for i, pid in enumerate(product_ids):
            try:
                cats = await self._client.products.get_categories(pid)
            except RohlikAPIError as err:
                _LOGGER.debug("Failed to fetch categories for product %s: %s", pid, err)
                cats = []
            if cats is None:
                # Product discontinued (404) - mark with sentinel category.
                results[pid] = [{"level": 1, "name": "Discontinued"}]
            elif cats:
                results[pid] = cats
            if progress_callback and i and i % 50 == 0:
                await progress_callback(i, total)
            await asyncio.sleep(0.2)
        _LOGGER.info("Category fetch complete: %d/%d products", len(results), total)
        return results

    async def refresh_slots(self) -> None:
        """Cheaply refresh only the delivery-slot data (for express-slot polling).

        Updates self.data["next_delivery_slot"] in place and notifies entities,
        without disturbing the rest of the data or the regular refresh cycle.
        """
        if not self.data:
            return
        result = await self._client.delivery.get_next_slots()
        if result is not None:
            self.data["next_delivery_slot"] = result
            self.async_update_listeners()

    async def refresh_delivery_times(self) -> None:
        """Cheaply refresh the delivery tracking and/or announcements (ETA).

        Light enough to poll every minute shortly before a delivery. While
        tracking has a courier estimate the announcement request is skipped.
        Updates the hub's tracking and
        self.data["delivery_announcements"] in place and notifies entities
        only if something they show changed.
        """
        if not self.data:
            return
        tracking_changed, just_delivered = await self._async_refresh_tracking(self.data)
        tracking = self.tracking
        announcements = None
        if tracking is None or tracking.estimated_arrival is None:
            announcements = await self._client.delivery.get_announcements()
        changed = tracking_changed
        if announcements is not None and announcements != self.data.get("delivery_announcements"):
            # Unchanged announcements are skipped: notifying would make every
            # entity of the account recompute its state for nothing.
            self.data["delivery_announcements"] = announcements
            changed = True
        if changed:
            self.async_update_listeners()
        if just_delivered and not self._closed:
            # The order leaves the upcoming list right after delivery: pick
            # that up now instead of at the next 10-minute refresh.
            await self.async_request_refresh()

    async def async_close(self) -> None:
        """Release resources held by the API client (called on unload)."""
        self._closed = True
        for unsub in (self._eta_stale_unsub, self._eta_poll_unsub):
            if unsub is not None:
                unsub()
        self._eta_stale_unsub = self._eta_poll_unsub = None
        await self._eta.async_close()
        # Logs out (best-effort) and leaves the injected session alone. The
        # session came from async_create_clientsession during entry setup, so
        # Home Assistant detaches it itself when the entry unloads - closing it
        # here would only trip the "custom integration closes the Home
        # Assistant aiohttp session" warning.
        await self._client.close()

    # New service methods
    async def add_to_cart(self, product_id: int, quantity: int) -> dict:
        """Add a product to the shopping cart."""
        added = await self._client.cart.add_items(
            [{"product_id": product_id, "quantity": quantity}]
        )
        await self.async_update()
        return {"added_products": added}

    async def search_product(self, product_name: str, limit: int = 10, favourite: bool = False):
        """Search for a product by name. Returns a SearchResults model (or None)."""
        return await self._client.products.search(product_name, limit, favourite)

    async def get_shopping_list(self, shopping_list_id: str):
        """Get a shopping list by ID. Returns a ShoppingList model."""
        return await self._client.account.get_shopping_list(shopping_list_id)

    async def get_cart_content(self):
        """Retrieve cart content. Returns a Cart model."""
        return await self._client.cart.get_content()

    async def search_and_add(self, product_name: str, quantity: int, favourite: bool = False) -> dict | None:
        """Search for a product by name and add the top match to the cart."""
        results = await self.search_product(product_name, limit=5, favourite=favourite)

        if results and results.results:
            first = results.results[0]
            await self.add_to_cart(first.id, quantity)
            return {"success": True, "message": "", "added_to_cart": [asdict(first)]}

        in_fav = " in favourites" if favourite else ""
        return {
            "success": False,
            "message": f'No product matched when searching for "{product_name}"{in_fav}.',
            "added_to_cart": [],
        }

    async def delete_from_cart(self, order_field_id: str) -> None:
        """Delete a product from the shopping cart using orderFieldId."""
        await self._client.cart.delete_item(order_field_id)
        await self.async_update()  # Refresh data after deletion