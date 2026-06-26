"""Platform for calendar integration."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, ICON_DELIVERY_CALENDAR
from .entity import BaseEntity
from .hub import RohlikAccount
from .utils import parse_orders_for_calendar

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up calendar entities for passed config_entry in HA."""
    rohlik_hub: RohlikAccount = config_entry.runtime_data
    async_add_entities([RohlikDeliveryCalendar(rohlik_hub)])


class RohlikDeliveryCalendar(BaseEntity, CalendarEntity, RestoreEntity):
    """Calendar entity for Rohlik.cz delivery windows."""

    _attr_translation_key = "delivery_calendar"
    _attr_should_poll = False
    _attr_icon = ICON_DELIVERY_CALENDAR

    def __init__(self, rohlik_account: RohlikAccount) -> None:
        """Initialize the calendar entity."""
        super().__init__(rohlik_account)
        self._events: list[CalendarEvent] = []
        self._events_by_order_id: dict[str, CalendarEvent] = {}  # Track events by order ID
        self._stored_delivery_slots: dict[str, dict[str, str]] = {}  # order_id -> {start: iso, end: iso}

    def _extract_orders_list(self, data_key: str) -> list:
        """Extract orders list from API response, handling wrapped responses."""
        raw_data = self._rohlik_account.data.get(data_key)
        
        if raw_data is None:
            _LOGGER.debug("No data found for key: %s", data_key)
            return []
        
        # If it's already a list, return it
        if isinstance(raw_data, list):
            _LOGGER.debug("Found %d orders in %s", len(raw_data), data_key)
            return raw_data
        
        # If it's a dict, try to extract the list from common response structures
        if isinstance(raw_data, dict):
            # Try common response wrapper keys
            for key in ["data", "orders", "items", "results"]:
                if key in raw_data and isinstance(raw_data[key], list):
                    _LOGGER.debug("Extracted %d orders from %s.%s", len(raw_data[key]), data_key, key)
                    return raw_data[key]
            _LOGGER.warning("Expected list in %s but got dict without list field: %s", data_key, list(raw_data.keys()))
            return []
        
        _LOGGER.warning("Unexpected data type for %s: %s", data_key, type(raw_data))
        return []

    def _update_events(self) -> None:
        """Rebuild the events list from current order data."""
        next_orders = self._extract_orders_list("next_order")
        delivered_orders = self._extract_orders_list("delivered_orders")

        _LOGGER.debug(
            "Updating calendar events - next_orders: %d, delivered_orders: %d",
            len(next_orders),
            len(delivered_orders),
        )

        # Get all order IDs from both sources (for tracking which orders still exist)
        all_order_ids = set()
        next_order_ids = set()
        delivered_order_ids = set()
        for order in (next_orders or []):
            order_id = order.get('id')
            if order_id:
                order_id_str = str(order_id)
                all_order_ids.add(order_id_str)
                next_order_ids.add(order_id_str)
        for order in (delivered_orders or []):
            order_id = order.get('id')
            if order_id:
                order_id_str = str(order_id)
                all_order_ids.add(order_id_str)
                delivered_order_ids.add(order_id_str)

        # Remove events for orders that are no longer in either list
        orders_to_remove = set(self._events_by_order_id.keys()) - all_order_ids
        for order_id in orders_to_remove:
            del self._events_by_order_id[order_id]
            _LOGGER.debug("Removed calendar event for order %s (no longer in next_order or delivered_orders)", order_id)

        # Parse orders from next_order only (these have delivery slots)
        # delivered_orders don't have delivery slots, so we can't create events from them
        # but we keep existing events for orders that moved to delivered_orders
        normalized_orders = parse_orders_for_calendar(next_orders, [])
        _LOGGER.debug("Parsed %d normalized orders from next_order", len(normalized_orders))

        # Create or update events for orders in next_order (they have delivery slots)
        for order in normalized_orders:
            order_id = order['id']
            try:
                # Store delivery slot info for persistence (needed for delivered orders after restart)
                self._stored_delivery_slots[order_id] = {
                    "start": order["start"].isoformat(),
                    "end": order["end"].isoformat(),
                }
                
                # Build description with optional details
                description_parts = []
                if order.get("status"):
                    description_parts.append(f"Status: {order['status']}")
                if order.get("items_count") is not None:
                    description_parts.append(f"Items: {order['items_count']}")
                if order.get("price") is not None:
                    description_parts.append(f"Price: {order['price']} CZK")
                description = "\n".join(description_parts) if description_parts else None

                event = CalendarEvent(
                    start=order["start"],
                    end=order["end"],
                    summary=f"Order {order['id']}",
                    description=description,
                    uid=str(order["id"]),
                )
                
                # Update or add event
                if order_id in self._events_by_order_id:
                    _LOGGER.debug("Updating calendar event for order %s", order_id)
                else:
                    _LOGGER.debug(
                        "Created calendar event for order %s: %s to %s",
                        order_id,
                        order["start"],
                        order["end"]
                    )
                
                self._events_by_order_id[order_id] = event
                
            except (KeyError, TypeError) as e:
                _LOGGER.warning("Error creating calendar event for order %s: %s", order.get('id'), e)
                continue
        
        # Recreate events for delivered orders that don't have delivery slot info
        # but we have stored delivery slot info from when they were in next_order
        for order in (delivered_orders or []):
            order_id = str(order.get('id', ''))
            if not order_id:
                continue
            
            # Update existing event to mark as delivered if needed
            if order_id in self._events_by_order_id:
                existing_event = self._events_by_order_id[order_id]
                # Check if order is delivered (in delivered_orders but not in next_orders)
                if order_id in delivered_order_ids and order_id not in next_order_ids:
                    if not existing_event.summary.startswith("[Delivered]"):
                        # Update the event summary to include delivered tag
                        new_summary = f"[Delivered] {existing_event.summary}"
                        self._events_by_order_id[order_id] = CalendarEvent(
                            start=existing_event.start,
                            end=existing_event.end,
                            summary=new_summary,
                            description=existing_event.description,
                            uid=existing_event.uid,
                        )
                        _LOGGER.debug("Tagged order %s as delivered", order_id)
                continue
            
            # Check if we have stored delivery slot info for this order
            if order_id in self._stored_delivery_slots:
                try:
                    slot_info = self._stored_delivery_slots[order_id]
                    start_dt = dt_util.parse_datetime(slot_info["start"])
                    end_dt = dt_util.parse_datetime(slot_info["end"])
                    
                    if start_dt and end_dt:
                        # Build description with optional details
                        description_parts = []
                        if order.get("status"):
                            description_parts.append(f"Status: {order['status']}")
                        if order.get("itemsCount") is not None:
                            description_parts.append(f"Items: {order['itemsCount']}")
                        price_amount = order.get("priceComposition", {}).get("total", {}).get("amount")
                        if price_amount is not None:
                            description_parts.append(f"Price: {price_amount} CZK")
                        description = "\n".join(description_parts) if description_parts else None
                        
                        event = CalendarEvent(
                            start=start_dt,
                            end=end_dt,
                            summary=f"[Delivered] Order {order_id}",
                            description=description,
                            uid=order_id,
                        )
                        
                        self._events_by_order_id[order_id] = event
                        _LOGGER.debug(
                            "Recreated calendar event for delivered order %s using stored delivery slot: %s to %s",
                            order_id,
                            start_dt,
                            end_dt
                        )
                except (KeyError, TypeError, ValueError) as e:
                    _LOGGER.warning("Error recreating calendar event for delivered order %s: %s", order_id, e)
                    continue
        
        # Clean up stored delivery slots for orders that no longer exist
        orders_to_remove_slots = set(self._stored_delivery_slots.keys()) - all_order_ids
        for order_id in orders_to_remove_slots:
            del self._stored_delivery_slots[order_id]
            _LOGGER.debug("Removed stored delivery slot for order %s (no longer in next_order or delivered_orders)", order_id)

        # Rebuild events list from dict (includes both new events and kept events)
        self._events = list(self._events_by_order_id.values())
        # Sort by start time
        self._events.sort(key=lambda x: x.start)

        _LOGGER.info("Calendar updated with %d events", len(self._events))

    @property
    def event(self) -> CalendarEvent | None:
        """Return the current or next upcoming event."""
        if not self._events:
            return None

        now = dt_util.now()
        
        # Find current active event (start <= now < end)
        for event in self._events:
            if event.start <= now < event.end:
                return event
        
        # Find next upcoming event (start > now)
        for event in self._events:
            if event.start > now:
                return event
        
        # No current or upcoming events
        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return calendar events within a datetime range."""
        if not self._events:
            return []

        # Filter events that intersect the range
        # start_date is lower bound applied to event's end (exclusive)
        # end_date is upper bound applied to event's start (exclusive)
        filtered_events = [
            event
            for event in self._events
            if event.end > start_date and event.start < end_date
        ]

        # Return sorted by start time
        filtered_events.sort(key=lambda x: x.start)
        return filtered_events

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()
        
        # Restore stored delivery slot information from previous session
        if (last_state := await self.async_get_last_state()) is not None:
            stored_slots = last_state.attributes.get("stored_delivery_slots", {})
            if stored_slots:
                self._stored_delivery_slots = stored_slots
                _LOGGER.debug("Restored %d stored delivery slots from previous session", len(stored_slots))
        
        # Initial update
        _LOGGER.debug("Calendar entity added to hass, updating events")
        self._update_events()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes to persist delivery slot information."""
        return {
            "stored_delivery_slots": self._stored_delivery_slots,
        }

    def _handle_coordinator_update(self) -> None:
        """Rebuild events when the coordinator delivers fresh data."""
        _LOGGER.debug("Calendar received coordinator update")
        self._update_events()
        self.async_write_ha_state()

