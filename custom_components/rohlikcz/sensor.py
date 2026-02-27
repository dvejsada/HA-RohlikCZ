"""Platform for sensor integration."""
from __future__ import annotations

import logging
import re
import datetime

from collections.abc import Mapping
from datetime import timedelta, datetime, time
from typing import Any
from zoneinfo import ZoneInfo
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
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
from .hub import RohlikAccount
from .utils import extract_delivery_datetime, get_earliest_order, parse_delivery_datetime_string

SCAN_INTERVAL = timedelta(seconds=600)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    """Add sensors for passed config_entry in HA."""
    rohlik_hub: RohlikAccount = hass.data[DOMAIN][config_entry.entry_id]  # type: ignore[Any]
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

    # Category sensors per selected level
    if "categories_l0" in analytics:
        entities.append(CategorySpendingL0Yearly(rohlik_hub))
        entities.append(CategorySpendingL0AllTime(rohlik_hub))
    if "categories_l1" in analytics:
        entities.append(CategorySpendingYearly(rohlik_hub))
        entities.append(CategorySpendingAllTime(rohlik_hub))
    if "categories_l2" in analytics:
        entities.append(CategorySpendingL2Yearly(rohlik_hub))
        entities.append(CategorySpendingL2AllTime(rohlik_hub))
    if "categories_l3" in analytics:
        entities.append(CategorySpendingL3Yearly(rohlik_hub))
        entities.append(CategorySpendingL3AllTime(rohlik_hub))
    if "per_item" in analytics:
        entities.append(ItemSpendingYearly(rohlik_hub))
        entities.append(ItemSpendingAllTime(rohlik_hub))

    if rohlik_hub.has_address:
        entities.append(FirstExpressSlot(rohlik_hub))
        entities.append(FirstStandardSlot(rohlik_hub))
        entities.append(FirstEcoSlot(rohlik_hub))

    # Only add premium days remaining if the user is premium
    if rohlik_hub.data.get('login', {}).get('data', {}).get('user', {}).get('premium', {}).get('active', False):
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
        
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)

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
        """Returns time of delivery."""
        delivery_info: list = self._rohlik_account.data["delivery_announcements"]["data"]["announcements"]
        if len(delivery_info) > 0:
            delivery_time = extract_delivery_datetime(delivery_info[0].get("content", ""))
            self._last_value = delivery_time
            return delivery_time
        else:
            # If announcements stopped but order still exists, preserve last value
            if self._rohlik_account.is_ordered and self._last_value is not None:
                return self._last_value
            else:
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
        
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)

class FirstExpressSlot(BaseEntity, SensorEntity):
    """Sensor for first available delivery."""

    _attr_translation_key = "express_slot"
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        """Returns datetime of the express slot."""
        preselected_slots = self._rohlik_account.data["next_delivery_slot"].get('data', {}).get('preselectedSlots', [])
        state = None
        for slot in preselected_slots:
            if slot.get("type", "") == "EXPRESS":
                state = datetime.strptime(slot.get("slot", {}).get("interval", {}).get("since", None),
                                          "%Y-%m-%dT%H:%M:%S%z")
                break
        return state

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Returns extra state attributes."""
        preselected_slots = self._rohlik_account.data["next_delivery_slot"].get('data', {}).get('preselectedSlots', [])
        extra_attrs = None
        for slot in preselected_slots:
            if slot.get("type", "") == "EXPRESS":
                extra_attrs = {
                    "Delivery Slot End": datetime.strptime(slot.get("slot", {}).get("interval", {}).get("till", None),
                                                           "%Y-%m-%dT%H:%M:%S%z"),
                    "Remaining Capacity Percent": int(
                        slot.get("slot", {}).get("timeSlotCapacityDTO", {}).get("totalFreeCapacityPercent", 0)),
                    "Remaining Capacity Message": slot.get("slot", {}).get("timeSlotCapacityDTO", {}).get(
                        "capacityMessage", None),
                    "Price": int(slot.get("price", 0)),
                    "Title": slot.get("title", None),
                    "Subtitle": slot.get("subtitle", None)
                }
                break

        return extra_attrs

    @property
    def entity_picture(self) -> str | None:
        return  "https://cdn.rohlik.cz/images/icons/preselected-slots/express.png"


    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class FirstStandardSlot(BaseEntity, SensorEntity):
    """Sensor for first available delivery."""

    _attr_translation_key = "standard_slot"
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        """Returns datetime of the standard slot."""
        preselected_slots = self._rohlik_account.data["next_delivery_slot"].get('data', {}).get('preselectedSlots', [])
        state = None
        for slot in preselected_slots:
            if slot.get("type", "") == "FIRST":
                state = datetime.strptime(slot.get("slot", {}).get("interval", {}).get("since", None),
                                          "%Y-%m-%dT%H:%M:%S%z")
                break
        return state

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Returns extra state attributes."""
        preselected_slots = self._rohlik_account.data["next_delivery_slot"].get('data', {}).get('preselectedSlots', [])
        extra_attrs = None
        for slot in preselected_slots:
            if slot.get("type", "") == "FIRST":
                extra_attrs = {
                    "Delivery Slot End": datetime.strptime(slot.get("slot", {}).get("interval", {}).get("till", None),
                                                           "%Y-%m-%dT%H:%M:%S%z"),
                    "Remaining Capacity Percent": int(
                        slot.get("slot", {}).get("timeSlotCapacityDTO", {}).get("totalFreeCapacityPercent", 0)),
                    "Remaining Capacity Message": slot.get("slot", {}).get("timeSlotCapacityDTO", {}).get(
                        "capacityMessage", None),
                    "Price": int(slot.get("price", 0)),
                    "Title": slot.get("title", None),
                    "Subtitle": slot.get("subtitle", None)
                    }
                break

        return extra_attrs

    @property
    def entity_picture(self) -> str | None:
        return  "https://cdn.rohlik.cz/images/icons/preselected-slots/first.png"

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class FirstEcoSlot(BaseEntity, SensorEntity):
    """Sensor for first available delivery."""

    _attr_translation_key = "eco_slot"
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        """Returns datetime of the eco slot."""
        preselected_slots = self._rohlik_account.data["next_delivery_slot"].get('data', {}).get('preselectedSlots', [])
        state = None
        for slot in preselected_slots:
            if slot.get("type", "") == "ECO":
                state = datetime.strptime(slot.get("slot", {}).get("interval", {}).get("since", None), "%Y-%m-%dT%H:%M:%S%z")
                break
        return state

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Returns extra state attributes."""
        preselected_slots = self._rohlik_account.data["next_delivery_slot"].get('data', {}).get('preselectedSlots', [])
        extra_attrs = None
        for slot in preselected_slots:
            if slot.get("type", "") == "ECO":
                extra_attrs = {"Delivery Slot End": datetime.strptime(slot.get("slot", {}).get("interval", {}).get("till", None), "%Y-%m-%dT%H:%M:%S%z"),
                    "Remaining Capacity Percent": int(slot.get("slot", {}).get("timeSlotCapacityDTO", {}).get("totalFreeCapacityPercent", 0)),
                    "Remaining Capacity Message": slot.get("slot", {}).get("timeSlotCapacityDTO", {}).get("capacityMessage", None),
                    "Price": int(slot.get("price", 0)),
                    "Title": slot.get("title", None),
                    "Subtitle": slot.get("subtitle", None)
                    }
                break

        return extra_attrs

    @property
    def entity_picture(self) -> str | None:
        return  "https://cdn.rohlik.cz/images/icons/preselected-slots/eco.png"

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class FirstDeliverySensor(BaseEntity, SensorEntity):
    """Sensor for first available delivery."""

    _attr_translation_key = "first_delivery"
    _attr_should_poll = False

    @property
    def native_value(self) -> str:
        """Returns first available delivery time."""
        return self._rohlik_account.data.get('delivery', {}).get('data', {}).get('firstDeliveryText', {}).get('default', 'Unknown')

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Returns delivery location."""
        delivery_data = self._rohlik_account.data.get('delivery', {}).get('data', {})
        if delivery_data:
            return {
                "delivery_location": delivery_data.get('deliveryLocationText', ''),
                "delivery_type": delivery_data.get('deliveryType', '')
            }
        return None

    @property
    def icon(self) -> str:
        return ICON_DELIVERY

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


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

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


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

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


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

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


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

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


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
        
        self._rohlik_account.register_callback(self.async_write_ha_state)

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
        self._check_and_reset_month()
        self._process_new_orders()
        return self._monthly_total if self._monthly_total > 0 else 0.0

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

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


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

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


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
        }

    @property
    def icon(self) -> str:
        return ICON_ALLTIME_SPENT

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class CategorySpendingYearly(BaseEntity, SensorEntity):
    """Sensor for spending breakdown by category for current year."""

    _attr_translation_key = "categories_this_year"
    _attr_should_poll = False

    @property
    def native_value(self) -> int | None:
        """Returns number of categories with spending this year."""
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        categories = store.category_totals(year=year, level=1, hide_discontinued=self._rohlik_account.hide_discontinued)
        return len(categories)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        categories = store.category_totals(year=year, level=1, hide_discontinued=self._rohlik_account.hide_discontinued)
        if not categories:
            return {"year": year, "enriched_orders": store.enriched_count, "total_orders": store.yearly_count(year)}
        return {
            "year": year,
            "total_count": len(categories),
            "categories": categories[:self._rohlik_account.top_n],
            "enriched_orders": store.enriched_count,
            "total_orders": store.yearly_count(year),
        }

    @property
    def icon(self) -> str:
        return ICON_CATEGORY_SPENDING

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class CategorySpendingAllTime(BaseEntity, SensorEntity):
    """Sensor for spending breakdown by category across all time."""

    _attr_translation_key = "categories_all_time"
    _attr_should_poll = False

    @property
    def native_value(self) -> int | None:
        """Returns number of categories with spending all time."""
        store = self._rohlik_account.order_store
        if not store:
            return None
        categories = store.category_totals(level=1, hide_discontinued=self._rohlik_account.hide_discontinued)
        return len(categories)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        categories = store.category_totals(level=1, hide_discontinued=self._rohlik_account.hide_discontinued)
        if not categories:
            return {"enriched_orders": store.enriched_count, "total_orders": store.alltime_count()}
        return {
            "total_count": len(categories),
            "categories": categories[:self._rohlik_account.top_n],
            "enriched_orders": store.enriched_count,
            "total_orders": store.alltime_count(),
            "products_in_cache": store.cached_product_count,
        }

    @property
    def icon(self) -> str:
        return ICON_CATEGORY_SPENDING

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class CategorySpendingL0Yearly(BaseEntity, SensorEntity):
    """Sensor for spending breakdown by L0 category for current year."""

    _attr_translation_key = "categories_l0_this_year"
    _attr_should_poll = False

    @property
    def native_value(self) -> int | None:
        """Returns number of L0 categories with spending this year."""
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        categories = store.category_totals(year=year, level=0, hide_discontinued=self._rohlik_account.hide_discontinued)
        return len(categories)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        categories = store.category_totals(year=year, level=0, hide_discontinued=self._rohlik_account.hide_discontinued)
        if not categories:
            return {"year": year, "enriched_orders": store.enriched_count, "total_orders": store.yearly_count(year)}
        return {
            "year": year,
            "total_count": len(categories),
            "categories": categories[:self._rohlik_account.top_n],
            "enriched_orders": store.enriched_count,
            "total_orders": store.yearly_count(year),
        }

    @property
    def icon(self) -> str:
        return ICON_CATEGORY_SPENDING

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class CategorySpendingL0AllTime(BaseEntity, SensorEntity):
    """Sensor for spending breakdown by L0 category across all time."""

    _attr_translation_key = "categories_l0_all_time"
    _attr_should_poll = False

    @property
    def native_value(self) -> int | None:
        """Returns number of L0 categories with spending all time."""
        store = self._rohlik_account.order_store
        if not store:
            return None
        categories = store.category_totals(level=0, hide_discontinued=self._rohlik_account.hide_discontinued)
        return len(categories)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        categories = store.category_totals(level=0, hide_discontinued=self._rohlik_account.hide_discontinued)
        if not categories:
            return {"enriched_orders": store.enriched_count, "total_orders": store.alltime_count()}
        return {
            "total_count": len(categories),
            "categories": categories[:self._rohlik_account.top_n],
            "enriched_orders": store.enriched_count,
            "total_orders": store.alltime_count(),
            "products_in_cache": store.cached_product_count,
        }

    @property
    def icon(self) -> str:
        return ICON_CATEGORY_SPENDING

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class CategorySpendingL2Yearly(BaseEntity, SensorEntity):
    """Sensor for spending breakdown by L2 category for current year."""

    _attr_translation_key = "categories_l2_this_year"
    _attr_should_poll = False

    @property
    def native_value(self) -> int | None:
        """Returns number of L2 categories with spending this year."""
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        categories = store.category_totals(year=year, level=2, hide_discontinued=self._rohlik_account.hide_discontinued)
        return len(categories)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        categories = store.category_totals(year=year, level=2, hide_discontinued=self._rohlik_account.hide_discontinued)
        if not categories:
            return {"year": year, "enriched_orders": store.enriched_count, "total_orders": store.yearly_count(year)}
        return {
            "year": year,
            "total_count": len(categories),
            "categories": categories[:self._rohlik_account.top_n],
            "enriched_orders": store.enriched_count,
            "total_orders": store.yearly_count(year),
        }

    @property
    def icon(self) -> str:
        return ICON_CATEGORY_SPENDING

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class CategorySpendingL2AllTime(BaseEntity, SensorEntity):
    """Sensor for spending breakdown by L2 category across all time."""

    _attr_translation_key = "categories_l2_all_time"
    _attr_should_poll = False

    @property
    def native_value(self) -> int | None:
        """Returns number of L2 categories with spending all time."""
        store = self._rohlik_account.order_store
        if not store:
            return None
        categories = store.category_totals(level=2, hide_discontinued=self._rohlik_account.hide_discontinued)
        return len(categories)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        categories = store.category_totals(level=2, hide_discontinued=self._rohlik_account.hide_discontinued)
        if not categories:
            return {"enriched_orders": store.enriched_count, "total_orders": store.alltime_count()}
        return {
            "total_count": len(categories),
            "categories": categories[:self._rohlik_account.top_n],
            "enriched_orders": store.enriched_count,
            "total_orders": store.alltime_count(),
            "products_in_cache": store.cached_product_count,
        }

    @property
    def icon(self) -> str:
        return ICON_CATEGORY_SPENDING

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class CategorySpendingL3Yearly(BaseEntity, SensorEntity):
    """Sensor for spending breakdown by L3 category for current year."""

    _attr_translation_key = "categories_l3_this_year"
    _attr_should_poll = False

    @property
    def native_value(self) -> int | None:
        """Returns number of L3 categories with spending this year."""
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        categories = store.category_totals(year=year, level=3, hide_discontinued=self._rohlik_account.hide_discontinued)
        return len(categories)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        categories = store.category_totals(year=year, level=3, hide_discontinued=self._rohlik_account.hide_discontinued)
        if not categories:
            return {"year": year, "enriched_orders": store.enriched_count, "total_orders": store.yearly_count(year)}
        return {
            "year": year,
            "total_count": len(categories),
            "categories": categories[:self._rohlik_account.top_n],
            "enriched_orders": store.enriched_count,
            "total_orders": store.yearly_count(year),
        }

    @property
    def icon(self) -> str:
        return ICON_CATEGORY_SPENDING

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class CategorySpendingL3AllTime(BaseEntity, SensorEntity):
    """Sensor for spending breakdown by L3 category across all time."""

    _attr_translation_key = "categories_l3_all_time"
    _attr_should_poll = False

    @property
    def native_value(self) -> int | None:
        """Returns number of L3 categories with spending all time."""
        store = self._rohlik_account.order_store
        if not store:
            return None
        categories = store.category_totals(level=3, hide_discontinued=self._rohlik_account.hide_discontinued)
        return len(categories)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        categories = store.category_totals(level=3, hide_discontinued=self._rohlik_account.hide_discontinued)
        if not categories:
            return {"enriched_orders": store.enriched_count, "total_orders": store.alltime_count()}
        return {
            "total_count": len(categories),
            "categories": categories[:self._rohlik_account.top_n],
            "enriched_orders": store.enriched_count,
            "total_orders": store.alltime_count(),
            "products_in_cache": store.cached_product_count,
        }

    @property
    def icon(self) -> str:
        return ICON_CATEGORY_SPENDING

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class ItemSpendingYearly(BaseEntity, SensorEntity):
    """Sensor for spending breakdown by individual item for current year."""

    _attr_translation_key = "items_this_year"
    _attr_should_poll = False

    @property
    def native_value(self) -> int | None:
        """Returns number of unique items purchased this year."""
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        items = store.item_totals(year=year, hide_discontinued=self._rohlik_account.hide_discontinued)
        return len(items)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        year = datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y")
        items = store.item_totals(year=year, hide_discontinued=self._rohlik_account.hide_discontinued)
        if not items:
            return {"year": year}
        return {
            "year": year,
            "total_count": len(items),
            "items": items[:self._rohlik_account.top_n],
        }

    @property
    def icon(self) -> str:
        return ICON_CATEGORY_SPENDING

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class ItemSpendingAllTime(BaseEntity, SensorEntity):
    """Sensor for spending breakdown by individual item across all time."""

    _attr_translation_key = "items_all_time"
    _attr_should_poll = False

    @property
    def native_value(self) -> int | None:
        """Returns number of unique items purchased all time."""
        store = self._rohlik_account.order_store
        if not store:
            return None
        items = store.item_totals(hide_discontinued=self._rohlik_account.hide_discontinued)
        return len(items)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        store = self._rohlik_account.order_store
        if not store:
            return None
        items = store.item_totals(hide_discontinued=self._rohlik_account.hide_discontinued)
        if not items:
            return None
        return {
            "total_count": len(items),
            "items": items[:self._rohlik_account.top_n],
            "products_in_cache": store.cached_product_count,
        }

    @property
    def icon(self) -> str:
        return ICON_CATEGORY_SPENDING

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class NoLimitOrders(BaseEntity, SensorEntity):
    """Sensor for remaining no limit orders."""

    _attr_translation_key = "no_limit"
    _attr_should_poll = False

    @property
    def native_value(self) -> int:
        """Returns remaining orders without limit."""
        return self._rohlik_account.data.get('login', {}).get('data', {}).get('user', {}).get('premium', {}).get('premiumLimits', {}).get('ordersWithoutPriceLimit', {}).get('remaining', 0)

    @property
    def icon(self) -> str:
        return ICON_NO_LIMIT

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class FreeExpressOrders(BaseEntity, SensorEntity):
    """Sensor for remaining free express orders."""

    _attr_translation_key = "free_express"
    _attr_should_poll = False

    @property
    def native_value(self) -> int:
        """Returns remaining free express orders."""
        return self._rohlik_account.data.get('login', {}).get('data', {}).get('user', {}).get('premium', {}).get('premiumLimits', {}).get('freeExpressLimit', {}).get('remaining', 0)

    @property
    def icon(self) -> str:
        return ICON_FREE_EXPRESS

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


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

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class PremiumDaysRemainingSensor(BaseEntity, SensorEntity):
    """Sensor for premium days remaining."""

    _attr_translation_key = "premium_days"
    _attr_should_poll = False

    @property
    def native_value(self) -> int:
        """Returns premium days remaining."""
        return self._rohlik_account.data.get('login', {}).get('data', {}).get('user', {}).get('premium', {}).get('remainingDays', 0)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Returns premium details."""
        premium_data = self._rohlik_account.data.get('login', {}).get('data', {}).get('user', {}).get('premium', {})
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

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class CartPriceSensor(BaseEntity, SensorEntity):
    """Sensor for total cart price."""

    _attr_translation_key = "cart_price"
    _attr_should_poll = False

    @property
    def native_value(self) -> float:
        """Returns total cart price."""
        return self._rohlik_account.data.get('cart', {}).get('total_price', 0.0)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Returns cart details."""
        cart_data = self._rohlik_account.data.get('cart', {})
        if cart_data:
            return {
                "Total items": cart_data.get('total_items', 0),
                "Can Order": cart_data.get('can_make_order', False)
            }
        return None

    @property
    def icon(self) -> str:
        return ICON_CART

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)

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

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)

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

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


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

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)


class UpdateSensor(BaseEntity, SensorEntity):
    """Sensor for API update."""

    _attr_translation_key = "updated"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = ICON_UPDATE
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime:
        return datetime.now(tz=ZoneInfo("Europe/Prague"))

    async def async_update(self) -> None:
        """Calls regular update of data from API."""
        await self._rohlik_account.async_update()

    async def async_added_to_hass(self) -> None:
        self._rohlik_account.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._rohlik_account.remove_callback(self.async_write_ha_state)