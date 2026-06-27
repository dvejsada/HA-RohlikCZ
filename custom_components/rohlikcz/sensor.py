"""Platform for sensor integration."""
from __future__ import annotations

import logging
import re

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .const import DOMAIN, ICON_UPDATE, ICON_CREDIT, ICON_NO_LIMIT, ICON_FREE_EXPRESS, ICON_DELIVERY, ICON_BAGS, \
    ICON_CART, ICON_ACCOUNT, ICON_EMAIL, ICON_PHONE, ICON_PREMIUM_DAYS, ICON_LAST_ORDER, ICON_NEXT_ORDER_SINCE, \
    ICON_NEXT_ORDER_TILL, ICON_INFO, ICON_DELIVERY_TIME, ICON_MONTHLY_SPENT, ICON_YEARLY_SPENT, ICON_ALLTIME_SPENT, \
    ICON_CATEGORY_SPENDING
from .entity import BaseEntity
from .hub import OrderStore, RohlikAccount
from .utils import extract_delivery_datetime, get_earliest_order, parse_delivery_datetime_string

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    """Add sensors for passed config_entry in HA."""
    rohlik_hub: RohlikAccount = config_entry.runtime_data
    analytics = rohlik_hub.analytics

    entities = [
        FirstDeliverySensor(rohlik_hub),
        AccountIDSensor(rohlik_hub),
        EmailSensor(rohlik_hub),
        PhoneSensor(rohlik_hub),
        NoLimitOrders(rohlik_hub),
        FreeExpressOrders(rohlik_hub),
        CreditAmount(rohlik_hub),
        BagsAmountSensor(rohlik_hub),
        CartPriceSensor(rohlik_hub),
        UpdateSensor(rohlik_hub),
        LastOrder(rohlik_hub),
        NextOrderTill(rohlik_hub),
        NextOrderSince(rohlik_hub),
        DeliveryInfo(rohlik_hub),
        DeliveryTime(rohlik_hub),
        MonthlySpent(rohlik_hub),
    ]

    # Spending sensors that need the order store (only if any analytics enabled)
    if analytics:
        entities.append(YearlySpent(rohlik_hub))
        entities.append(AllTimeSpent(rohlik_hub))

    # Category / item spending sensors per selected analytics option.
    for option, descriptions in SPENDING_DESCRIPTIONS.items():
        if option in analytics:
            entities.extend(SpendingBreakdownSensor(rohlik_hub, d) for d in descriptions)

    if rohlik_hub.has_address:
        entities.extend(FirstSlotSensor(rohlik_hub, d) for d in SLOT_DESCRIPTIONS)

    # Only add premium days remaining if the user is premium
    if (rohlik_hub.data.get('login', {}).get('data', {}).get('user', {}).get('premium') or {}).get('active', False):
        entities.append(PremiumDaysRemainingSensor(rohlik_hub))

    async_add_entities(entities)

class DeliveryInfo(BaseEntity, SensorEntity, RestoreEntity):
    """Sensor for showing delivery information."""

    _attr_translation_key = "delivery_info"
    _attr_should_poll = False

    def __init__(self, rohlik_account: RohlikAccount) -> None:
        """Initialize the delivery info sensor."""
        super().__init__(rohlik_account)
        self._last_value: str | None = None
        self._last_attributes: Mapping[str, Any] | None = None

    @property
    def native_value(self) -> str | None:
        """Returns text of announcement."""
        delivery_info: list = self._rohlik_account.data["delivery_announcements"]["data"]["announcements"]
        if len(delivery_info) > 0:
            clean_text = re.sub(r'<[^>]+>', '', delivery_info[0]["content"])
            self._last_value = clean_text
            return clean_text
        else:
            # If announcements stopped but order still exists, preserve last value
            if self._rohlik_account.is_ordered and self._last_value is not None:
                return self._last_value
            else:
                return None

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """ Get extra state attributes. """
        delivery_info: list = self._rohlik_account.data["delivery_announcements"]["data"]["announcements"]
        if len(delivery_info) > 0:
            delivery_time = extract_delivery_datetime(delivery_info[0].get("content", ""))

            if delivery_info[0].get("additionalContent", None):
                clean_text = delivery_info[0]["additionalContent"]
                additional_info = re.sub(r'<[^>]+>', '', clean_text)
            else:
                additional_info = None

            attrs = {
                "Delivery time (deprecated, use new entity)": delivery_time,
                "Order Id": str(delivery_info[0].get("id")),
                "Updated At": datetime.fromisoformat(delivery_info[0].get("updatedAt")),
                "Title": delivery_info[0].get("title"),
                "Additional Content": additional_info
            }
            self._last_attributes = attrs
            return attrs
        else:
            # If announcements stopped but order still exists, preserve last attributes
            if self._rohlik_account.is_ordered and self._last_attributes is not None:
                return self._last_attributes
            else:
                return None

    @property
    def icon(self) -> str:
        return ICON_INFO

    async def async_added_to_hass(self) -> None:
        """Restore state when added to HA."""
        await super().async_added_to_hass()

        # Restore last state if available
        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state not in (STATE_UNAVAILABLE, "unknown", "None"):
                self._last_value = last_state.state
            if last_state.attributes:
                self._last_attributes = dict(last_state.attributes)


class DeliveryTime(BaseEntity, SensorEntity, RestoreEntity):
    """Sensor for showing delivery time."""

    _attr_translation_key = "delivery_time"
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, rohlik_account: RohlikAccount) -> None:
        """Initialize the delivery time sensor."""
        super().__init__(rohlik_account)
        self._last_value: datetime | None = None

    @property
    def native_value(self) -> datetime | None:
        """Returns time of delivery for the soonest upcoming order.

        The precise delivery time comes from the delivery announcement, but with
        multiple concurrent orders the announcement may relate to a later order
        rather than the soonest one. In that case (or once the announcement has
        been cleared) we fall back to the delivery slot of the earliest upcoming
        order - the same source the delivery slot sensors use - which always
        reflects the soonest order.
        """
        announcements: list = self._rohlik_account.data["delivery_announcements"]["data"]["announcements"]
        upcoming_orders: list = self._rohlik_account.data.get("next_order", []) or []
        earliest_order = get_earliest_order(upcoming_orders)

        if len(announcements) > 0:
            announcement = announcements[0]
            # Only trust the announcement when there is a single upcoming order
            # (no ambiguity) or when it explicitly matches the soonest order.
            announcement_matches_soonest = (
                len(upcoming_orders) <= 1
                or earliest_order is None
                or str(announcement.get("id", "")) == str(earliest_order.get("id", ""))
            )
            if announcement_matches_soonest:
                delivery_time = extract_delivery_datetime(announcement.get("content", ""))
                if delivery_time is not None:
                    self._last_value = delivery_time
                    return delivery_time

        # Fall back to the delivery slot of the soonest order.
        if earliest_order is not None:
            slot_since = parse_delivery_datetime_string(
                earliest_order.get("deliverySlot", {}).get("since")
            )
            if slot_since is not None:
                self._last_value = slot_since
                return slot_since

        # No live data available - preserve the last known value while an order
        # still exists so it isn't cleared shortly before delivery.
        if self._rohlik_account.is_ordered and self._last_value is not None:
            return self._last_value
        return None

    @property
    def icon(self) -> str:
        return ICON_DELIVERY_TIME

    async def async_added_to_hass(self) -> None:
        """Restore state when added to HA."""
        await super().async_added_to_hass()

        # Restore last state if available
        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state not in (STATE_UNAVAILABLE, "unknown", "None"):
                # Try to parse the restored state
                try:
                    if isinstance(last_state.state, datetime):
                        self._last_value = last_state.state
                    elif isinstance(last_state.state, str):
                        # Try to parse ISO format
                        self._last_value = datetime.fromisoformat(last_state.state.replace('Z', '+00:00'))
                except (ValueError, AttributeError):
                    _LOGGER.debug(
                        "Failed to restore delivery time from last state %r",
                        last_state.state,
                    )


@dataclass(frozen=True, kw_only=True)
class SlotSensorDescription(SensorEntityDescription):
    """Describes a 'first available slot' sensor for a given slot type."""

    slot_type: str
    picture: str


#: One description per preselected delivery-slot type.
SLOT_DESCRIPTIONS: tuple[SlotSensorDescription, ...] = (
    SlotSensorDescription(key="express_slot", slot_type="EXPRESS",
                          picture="https://cdn.rohlik.cz/images/icons/preselected-slots/express.png"),
    SlotSensorDescription(key="standard_slot", slot_type="FIRST",
                          picture="https://cdn.rohlik.cz/images/icons/preselected-slots/first.png"),
    SlotSensorDescription(key="eco_slot", slot_type="ECO",
                          picture="https://cdn.rohlik.cz/images/icons/preselected-slots/eco.png"),
)


class FirstSlotSensor(BaseEntity, SensorEntity):
    """First available delivery slot of a given type (express / standard / eco)."""

    entity_description: SlotSensorDescription
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, rohlik_account: RohlikAccount, description: SlotSensorDescription) -> None:
        self.entity_description = description
        super().__init__(rohlik_account)

    def _slot(self) -> dict | None:
        preselected = ((self._rohlik_account.data.get("next_delivery_slot") or {}).get("data") or {}).get("preselectedSlots", [])
        for slot in preselected:
            if slot.get("type", "") == self.entity_description.slot_type:
                return slot
        return None

    @property
    def native_value(self) -> datetime | None:
        """Returns datetime of the slot start."""
        slot = self._slot()
        if not slot:
            return None
        return datetime.strptime(slot.get("slot", {}).get("interval", {}).get("since", None), "%Y-%m-%dT%H:%M:%S%z")

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Returns extra state attributes."""
        slot = self._slot()
        if not slot:
            return None
        capacity = slot.get("slot", {}).get("timeSlotCapacityDTO", {})
        return {
            "Delivery Slot End": datetime.strptime(slot.get("slot", {}).get("interval", {}).get("till", None), "%Y-%m-%dT%H:%M:%S%z"),
            "Remaining Capacity Percent": int(capacity.get("totalFreeCapacityPercent", 0)),
            "Remaining Capacity Message": capacity.get("capacityMessage", None),
            "Price": int(slot.get("price", 0)),
            "Title": slot.get("title", None),
            "Subtitle": slot.get("subtitle", None),
        }

    @property
    def entity_picture(self) -> str | None:
        return self.entity_description.picture


class FirstDeliverySensor(BaseEntity, SensorEntity):
    """Sensor for first available delivery."""

    _attr_translation_key = "first_delivery"
    _attr_should_poll = False

    @property
    def native_value(self) -> str:
        """Returns first available delivery time."""
        return ((self._rohlik_account.data.get('delivery') or {}).get('data') or {}).get('firstDeliveryText', {}).get('default', 'Unknown')

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Returns delivery location."""
        delivery_data = (self._rohlik_account.data.get('delivery') or {}).get('data') or {}
        if delivery_data:
            return {
                "delivery_location": delivery_data.get('deliveryLocationText', ''),
                "delivery_type": delivery_data.get('deliveryType', '')
            }
        return None

    @property
    def icon(self) -> str:
        return ICON_DELIVERY


class AccountIDSensor(BaseEntity, SensorEntity):
    """Sensor for account ID."""

    _attr_translation_key = "account_id"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    @property
    def native_value(self) -> int | str:
        """Returns account ID."""
        return self._rohlik_account.data.get('login', {}).get('data', {}).get('user', {}).get('id', "N/A")

    @property
    def icon(self) -> str:
        return ICON_ACCOUNT


class EmailSensor(BaseEntity, SensorEntity):
    """Sensor for email."""

    _attr_translation_key = "email"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    @property
    def native_value(self) -> str:
        """Returns email."""
        return self._rohlik_account.data.get('login', {}).get('data', {}).get('user', {}).get('email', 'N/A')

    @property
    def icon(self) -> str:
        return ICON_EMAIL


class PhoneSensor(BaseEntity, SensorEntity):
    """Sensor for phone number."""

    _attr_translation_key = "phone"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    @property
    def native_value(self) -> str:
        """Returns phone number."""
        return self._rohlik_account.data.get('login', {}).get('data', {}).get('user', {}).get('phone', 'N/A')

    @property
    def icon(self) -> str:
        return ICON_PHONE


class CreditAmount(BaseEntity, SensorEntity):
    """Sensor for credit amount."""

    _attr_translation_key = "credit_amount"
    _attr_should_poll = False

    @property
    def native_value(self) -> float | str:
        """Returns amount of credit as state."""
        return self._rohlik_account.data.get('login', {}).get('data', {}).get('user', {}).get('credits', "N/A")

    @property
    def icon(self) -> str:
        return ICON_CREDIT


class MonthlySpent(BaseEntity, SensorEntity, RestoreEntity):
    """Sensor for amount spent in current month with HA-side accumulation.

    Only tracks orders that are delivered and closed (have final price).
    Orders from the delivered_orders endpoint should all be finalized.
    Uses Home Assistant's restore state to persist monthly totals across restarts.
    """

    _attr_translation_key = "monthly_spent"
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, rohlik_account: RohlikAccount) -> None:
        super().__init__(rohlik_account)
        self._monthly_total: float = 0.0
        self._processed_orders: set[str] = set()  # Store order IDs
        self._current_month: str = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y-%m")
        self._last_reset: datetime | None = None

    def _is_order_final(self, order: dict) -> bool:
        """
        Verify order has a final price.

        Since orders come from the 'delivered_orders' endpoint, they should be finalized.
        We verify by checking that priceComposition exists and has a valid amount.
        """
        # Check if priceComposition exists
        price_comp = order.get('priceComposition')
        if not price_comp:
            return False

        # Check if total exists
        total = price_comp.get('total')
        if not total:
            return False

        # Check if amount exists and is a valid number
        amount = total.get('amount')
        if amount is None:
            return False

        # Verify it's a valid number
        try:
            float(amount)
            return True
        except (ValueError, TypeError):
            return False

    async def async_added_to_hass(self) -> None:
        """Restore state when added to HA."""
        await super().async_added_to_hass()

        if (last_state := await self.async_get_last_state()) is not None:
            self._monthly_total = last_state.attributes.get("monthly_total", 0.0)
            self._processed_orders = set(last_state.attributes.get("processed_orders", []))
            self._current_month = last_state.attributes.get("current_month", datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y-%m"))
            if last_reset_str := last_state.attributes.get("last_reset"):
                self._last_reset = datetime.fromisoformat(last_reset_str)

        self._check_and_reset_month()
        self._process_new_orders()


    def _check_and_reset_month(self) -> None:
        """Reset total if month changed."""
        current_month = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y-%m")
        if current_month != self._current_month:
            _LOGGER.info(f"Month changed from {self._current_month} to {current_month}, resetting monthly total")
            self._monthly_total = 0.0
            self._processed_orders = set()
            self._current_month = current_month
            self._last_reset = datetime.now(ZoneInfo("Europe/Prague"))

    def _process_new_orders(self) -> None:
        """Process new orders and add to total.

        Only processes orders that are delivered and closed (have final price).
        Uses order ID for unique identification.
        """
        orders = self._rohlik_account.data.get('delivered_orders', [])
        if not orders:
            return

        current_month_pattern = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y-%m-")
        new_orders_count = 0

        for order in orders:
            try:
                order_time = order.get('orderTime', '')

                # Only process orders from current month
                if current_month_pattern not in order_time:
                    continue

                # Verify order has final price (delivered and closed)
                if not self._is_order_final(order):
                    _LOGGER.debug(f"Order {order.get('id')} does not have final price, skipping")
                    continue

                # Get order ID (unique identifier)
                order_id = order.get('id')
                if not order_id:
                    _LOGGER.warning(f"Order missing ID, skipping: {order.get('orderTime')}")
                    continue

                order_key = str(order_id)

                # Skip if already processed
                if order_key in self._processed_orders:
                    continue

                # Get the final price
                amount = float(order['priceComposition']['total']['amount'])

                # Add to total and mark as processed
                self._monthly_total += amount
                self._processed_orders.add(order_key)
                new_orders_count += 1

                _LOGGER.debug(f"Added order {order_id} with amount {amount} CZK. New total: {self._monthly_total} CZK")

            except (KeyError, ValueError, TypeError) as e:
                _LOGGER.warning(f"Skipping order due to error: {e}, order ID: {order.get('id')}")
                continue

        if new_orders_count > 0:
            _LOGGER.info(f"Processed {new_orders_count} new order(s). Monthly total: {self._monthly_total} CZK")

    @property
    def native_value(self) -> float | None:
        """Returns amount spent in current month."""
        return self._monthly_total if self._monthly_total > 0 else 0.0

    def _handle_coordinator_update(self) -> None:
        """Process newly delivered orders on each coordinator refresh."""
        self._check_and_reset_month()
        self._process_new_orders()
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Store state for restoration."""
        count = len(self._processed_orders)
        return {
            "monthly_total": self._monthly_total,
            "processed_orders": list(self._processed_orders),
            "current_month": self._current_month,
            "last_reset": self._last_reset.isoformat() if self._last_reset else None,
            "processed_count": count,
            "average_order_value": round(self._monthly_total / count, 2) if count > 0 else 0.0
        }

    @property
    def icon(self) -> str:
        return ICON_MONTHLY_SPENT


class YearlySpent(BaseEntity, SensorEntity):
    """Sensor for amount spent in current year from persistent order store."""

    _attr_translation_key = "yearly_spent"
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.TOTAL

    @property
    def native_value(self) -> float | None:
        """Returns amount spent in current year."""
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        return store.yearly_total(year)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        count = store.yearly_count(year)
        total = store.yearly_total(year)
        return {
            "year": year,
            "order_count": count,
            "average_order_value": round(total / count, 2) if count > 0 else 0.0,
        }

    @property
    def icon(self) -> str:
        return ICON_YEARLY_SPENT


class AllTimeSpent(BaseEntity, SensorEntity):
    """Sensor for total amount spent across all tracked orders."""

    _attr_translation_key = "alltime_spent"
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> float | None:
        """Returns total amount spent across all orders."""
        store = self._rohlik_account.order_store
        if not store:
            return None
        return store.alltime_total()

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        count = store.alltime_count()
        total = store.alltime_total()
        return {
            "order_count": count,
            "average_order_value": round(total / count, 2) if count > 0 else 0.0,
            "first_order_date": store.first_order_date(),
            "tracking_since": store.tracking_since,
            "by_year": store.yearly_breakdown(),
        }

    @property
    def icon(self) -> str:
        return ICON_ALLTIME_SPENT


@dataclass(frozen=True, kw_only=True)
class SpendingBreakdownDescription(SensorEntityDescription):
    """Describes a category/item spending-breakdown sensor."""

    kind: Literal["category", "item"]
    period: Literal["year", "alltime"]
    level: int | None = None


#: Analytics option -> (this-year description, all-time description).
SPENDING_DESCRIPTIONS: dict[str, tuple[SpendingBreakdownDescription, ...]] = {
    "categories_l0": (
        SpendingBreakdownDescription(key="categories_l0_this_year", kind="category", level=0, period="year"),
        SpendingBreakdownDescription(key="categories_l0_all_time", kind="category", level=0, period="alltime"),
    ),
    "categories_l1": (
        SpendingBreakdownDescription(key="categories_this_year", kind="category", level=1, period="year"),
        SpendingBreakdownDescription(key="categories_all_time", kind="category", level=1, period="alltime"),
    ),
    "categories_l2": (
        SpendingBreakdownDescription(key="categories_l2_this_year", kind="category", level=2, period="year"),
        SpendingBreakdownDescription(key="categories_l2_all_time", kind="category", level=2, period="alltime"),
    ),
    "categories_l3": (
        SpendingBreakdownDescription(key="categories_l3_this_year", kind="category", level=3, period="year"),
        SpendingBreakdownDescription(key="categories_l3_all_time", kind="category", level=3, period="alltime"),
    ),
    "per_item": (
        SpendingBreakdownDescription(key="items_this_year", kind="item", period="year"),
        SpendingBreakdownDescription(key="items_all_time", kind="item", period="alltime"),
    ),
}


class SpendingBreakdownSensor(BaseEntity, SensorEntity):
    """Generic spending-breakdown sensor for categories or items, by year or all time.

    The state is the number of distinct categories/items with spending; the
    breakdown (top N) and order/enrichment stats are exposed as attributes.
    """

    entity_description: SpendingBreakdownDescription
    _attr_should_poll = False
    _attr_icon = ICON_CATEGORY_SPENDING

    def __init__(self, rohlik_account: RohlikAccount, description: SpendingBreakdownDescription) -> None:
        self.entity_description = description
        super().__init__(rohlik_account)

    @staticmethod
    def _year() -> str:
        return datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")

    def _entries(self, store: OrderStore, year: str) -> list:
        d = self.entity_description
        hide = self._rohlik_account.hide_discontinued
        if d.kind == "category":
            if d.period == "year":
                return store.category_totals(year=year, level=d.level, hide_discontinued=hide)
            return store.category_totals(level=d.level, hide_discontinued=hide)
        if d.period == "year":
            return store.item_totals(year=year, hide_discontinued=hide)
        return store.item_totals(hide_discontinued=hide)

    @property
    def native_value(self) -> int | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        return len(self._entries(store, self._year()))

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        d = self.entity_description
        top_n = self._rohlik_account.top_n
        year = self._year()
        entries = self._entries(store, year)

        if d.kind == "category":
            if d.period == "year":
                base = {
                    "year": year,
                    "enriched_orders": store.yearly_enriched_count(year),
                    "total_orders": store.yearly_count(year),
                }
                if not entries:
                    return base
                return {**base, "total_count": len(entries), "categories": entries[:top_n]}
            # all time
            base = {"enriched_orders": store.enriched_count, "total_orders": store.alltime_count()}
            if not entries:
                return base
            return {
                "total_count": len(entries),
                "categories": entries[:top_n],
                **base,
                "products_in_cache": store.cached_product_count,
            }

        # item breakdown
        if d.period == "year":
            if not entries:
                return {"year": year}
            return {"year": year, "total_count": len(entries), "items": entries[:top_n]}
        if not entries:
            return None
        return {
            "total_count": len(entries),
            "items": entries[:top_n],
            "products_in_cache": store.cached_product_count,
        }


class NoLimitOrders(BaseEntity, SensorEntity):
    """Sensor for remaining no limit orders."""

    _attr_translation_key = "no_limit"
    _attr_should_poll = False

    @property
    def native_value(self) -> int:
        """Returns remaining orders without limit."""
        premium = self._rohlik_account.data.get('login', {}).get('data', {}).get('user', {}).get('premium') or {}
        return (premium.get('premiumLimits') or {}).get('ordersWithoutPriceLimit', {}).get('remaining', 0)

    @property
    def icon(self) -> str:
        return ICON_NO_LIMIT


class FreeExpressOrders(BaseEntity, SensorEntity):
    """Sensor for remaining free express orders."""

    _attr_translation_key = "free_express"
    _attr_should_poll = False

    @property
    def native_value(self) -> int:
        """Returns remaining free express orders."""
        premium = self._rohlik_account.data.get('login', {}).get('data', {}).get('user', {}).get('premium') or {}
        return (premium.get('premiumLimits') or {}).get('freeExpressLimit', {}).get('remaining', 0)

    @property
    def icon(self) -> str:
        return ICON_FREE_EXPRESS


class BagsAmountSensor(BaseEntity, SensorEntity):
    """Sensor for reusable bags amount."""

    _attr_translation_key = "bags_amount"
    _attr_should_poll = False

    @property
    def native_value(self) -> int:
        """Returns number of reusable bags."""
        return self._rohlik_account.data["bags"].get('current', 0)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Returns reusable bag details."""
        bags_data = self._rohlik_account.data["bags"]
        extra_attr: dict = {"Max Bags": bags_data.get('max', 0)}
        if bags_data.get('deposit', None):
            extra_attr["Deposit Amount"] = bags_data.get('deposit').get('amount', 0)
            extra_attr["Deposit Currency"] = bags_data.get('deposit').get('currency', 'CZK')
        return extra_attr

    @property
    def icon(self) -> str:
        return ICON_BAGS


class PremiumDaysRemainingSensor(BaseEntity, SensorEntity):
    """Sensor for premium days remaining."""

    _attr_translation_key = "premium_days"
    _attr_should_poll = False

    @property
    def native_value(self) -> int:
        """Returns premium days remaining."""
        return (self._rohlik_account.data.get('login', {}).get('data', {}).get('user', {}).get('premium') or {}).get('remainingDays', 0)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Returns premium details."""
        premium_data = self._rohlik_account.data.get('login', {}).get('data', {}).get('user', {}).get('premium') or {}
        if premium_data:
            return {
                "Premium Type": premium_data.get('premiumMembershipType', ''),
                "Payment Date": premium_data.get('recurrentPaymentDate', ''),
                "Start Date": premium_data.get('startDate', ''),
                "End Date": premium_data.get('endDate', '')
            }
        return None

    @property
    def icon(self) -> str:
        return ICON_PREMIUM_DAYS


class CartPriceSensor(BaseEntity, SensorEntity):
    """Sensor for total cart price."""

    _attr_translation_key = "cart_price"
    _attr_should_poll = False

    @property
    def native_value(self) -> float:
        """Returns total cart price."""
        cart = self._rohlik_account.data.get('cart')
        return cart.total_price if cart else 0.0

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Returns cart details."""
        cart = self._rohlik_account.data.get('cart')
        if cart:
            return {
                "Total items": cart.total_items,
                "Can Order": cart.can_make_order,
            }
        return None

    @property
    def icon(self) -> str:
        return ICON_CART


class NextOrderSince(BaseEntity, SensorEntity):
    """Sensor for start of delivery window of next order."""

    _attr_translation_key = "next_order_since"
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        """Returns start of delivery window for the earliest order."""
        earliest_order = get_earliest_order(self._rohlik_account.data.get('next_order', []))
        if earliest_order:
            since_str = earliest_order.get("deliverySlot", {}).get("since", None)
            return parse_delivery_datetime_string(since_str)
        return None

    @property
    def icon(self) -> str:
        return ICON_NEXT_ORDER_SINCE


class NextOrderTill(BaseEntity, SensorEntity):
    """Sensor for finish of delivery window of next order."""

    _attr_translation_key = "next_order_till"
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        """Returns end of delivery window for the earliest order."""
        earliest_order = get_earliest_order(self._rohlik_account.data.get('next_order', []))
        if earliest_order:
            till_str = earliest_order.get("deliverySlot", {}).get("till", None)
            return parse_delivery_datetime_string(till_str)
        return None

    @property
    def icon(self) -> str:
        return ICON_NEXT_ORDER_TILL


class LastOrder(BaseEntity, SensorEntity):
    """Sensor for datetime from last order."""

    _attr_translation_key = "last_order"
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime:
        """Returns remaining orders without limit."""
        return datetime.strptime(self._rohlik_account.data["last_order"][0].get("orderTime", None), "%Y-%m-%dT%H:%M:%S.%f%z")

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Returns last order details."""
        last_order_data = self._rohlik_account.data['last_order'][0]
        if len(last_order_data) > 0:
            return {
                "Items": last_order_data.get('itemsCount', None),
                "Price": last_order_data.get('priceComposition', {}).get('total', {}).get('amount', None),
            }
        return None


    @property
    def icon(self) -> str:
        return ICON_LAST_ORDER


class UpdateSensor(BaseEntity, SensorEntity):
    """Sensor for API update."""

    _attr_translation_key = "updated"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = ICON_UPDATE
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        """Time of the last data fetch from the API."""
        return self._rohlik_account.last_refresh
