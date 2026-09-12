"""Tests for Delivery Time source selection and live ETA preservation."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rohlikcz.const import DOMAIN

from fixtures_data import sample_api_data

ENTRY_DATA = {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"}


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, unique_id="123456", data=ENTRY_DATA, options={}
    )


def _order(order_id: int, slot_start: datetime) -> dict:
    return {
        "id": order_id,
        "deliverySlot": {
            "since": slot_start.isoformat(),
            "till": (slot_start + timedelta(hours=1)).isoformat(),
        },
    }


def _announcement(order_id: int, delivery_time: datetime) -> dict:
    return {
        "id": order_id,
        "title": "Delivery",
        "updatedAt": datetime.now(ZoneInfo("Europe/Prague")).isoformat(),
        "content": (
            'Doručíme v <span style="color:#009B37">'
            f"{delivery_time:%H:%M}</span>"
        ),
    }


async def _setup_delivery_time(
    hass: HomeAssistant, data: dict
) -> tuple[MockConfigEntry, str]:
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.rohlikcz.hub.RohlikAPI.get_data",
        new=AsyncMock(return_value=data),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_registry = er.async_get(hass)
    entity_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, "123456_delivery_time"
    )
    assert entity_id is not None
    return entry, entity_id


def _delivery_times() -> tuple[datetime, datetime]:
    now = datetime.now(ZoneInfo("Europe/Prague"))
    live_time = now.replace(second=0, microsecond=0) + timedelta(minutes=30)
    slot_start = live_time + timedelta(hours=2)
    return live_time, slot_start


def _state_time(hass: HomeAssistant, entity_id: str) -> datetime | None:
    state = hass.states.get(entity_id)
    assert state is not None
    return dt_util.parse_datetime(state.state)


async def test_preserves_live_eta_when_announcement_clears(
    hass: HomeAssistant,
) -> None:
    """A cleared announcement keeps the live ETA for the same active order."""
    live_time, slot_start = _delivery_times()
    data = sample_api_data()
    data["next_order"] = [_order(7001, slot_start)]
    data["delivery_announcements"]["data"]["announcements"] = [
        _announcement(7001, live_time)
    ]

    entry, entity_id = await _setup_delivery_time(hass, data)
    assert _state_time(hass, entity_id) == live_time

    updated_data = copy.deepcopy(data)
    updated_data["delivery_announcements"]["data"]["announcements"] = []
    entry.runtime_data.async_set_updated_data(updated_data)
    await hass.async_block_till_done()

    assert _state_time(hass, entity_id) == live_time


async def test_does_not_carry_live_eta_to_next_order(
    hass: HomeAssistant,
) -> None:
    """A new earliest order uses its slot instead of another order's live ETA."""
    live_time, slot_start = _delivery_times()
    data = sample_api_data()
    data["next_order"] = [_order(7001, slot_start)]
    data["delivery_announcements"]["data"]["announcements"] = [
        _announcement(7001, live_time)
    ]

    entry, entity_id = await _setup_delivery_time(hass, data)

    next_slot_start = slot_start + timedelta(days=1)
    updated_data = copy.deepcopy(data)
    updated_data["next_order"] = [_order(7002, next_slot_start)]
    updated_data["delivery_announcements"]["data"]["announcements"] = []
    entry.runtime_data.async_set_updated_data(updated_data)
    await hass.async_block_till_done()

    assert _state_time(hass, entity_id) == next_slot_start


async def test_preserves_live_eta_when_same_announcement_loses_time(
    hass: HomeAssistant,
) -> None:
    """A matching announcement without a time keeps that order's live ETA."""
    live_time, slot_start = _delivery_times()
    data = sample_api_data()
    data["next_order"] = [_order(7001, slot_start)]
    data["delivery_announcements"]["data"]["announcements"] = [
        _announcement(7001, live_time)
    ]

    entry, entity_id = await _setup_delivery_time(hass, data)

    updated_data = copy.deepcopy(data)
    updated_data["delivery_announcements"]["data"]["announcements"] = [
        {
            "id": 7001,
            "title": "Delivery",
            "updatedAt": datetime.now(ZoneInfo("Europe/Prague")).isoformat(),
            "content": "Kurýr je u vás.",
        }
    ]
    entry.runtime_data.async_set_updated_data(updated_data)
    await hass.async_block_till_done()

    assert _state_time(hass, entity_id) == live_time


async def test_concurrent_order_announcement_preserves_earliest_live_eta(
    hass: HomeAssistant,
) -> None:
    """A later order's announcement does not erase the earliest live ETA."""
    live_time, slot_start = _delivery_times()
    data = sample_api_data()
    data["next_order"] = [_order(7001, slot_start)]
    data["delivery_announcements"]["data"]["announcements"] = [
        _announcement(7001, live_time)
    ]

    entry, entity_id = await _setup_delivery_time(hass, data)

    later_slot_start = slot_start + timedelta(days=1)
    updated_data = copy.deepcopy(data)
    updated_data["next_order"] = [
        _order(7001, slot_start),
        _order(7002, later_slot_start),
    ]
    updated_data["delivery_announcements"]["data"]["announcements"] = [
        _announcement(7002, later_slot_start)
    ]
    entry.runtime_data.async_set_updated_data(updated_data)
    await hass.async_block_till_done()

    assert _state_time(hass, entity_id) == live_time

    cleared_data = copy.deepcopy(updated_data)
    cleared_data["delivery_announcements"]["data"]["announcements"] = []
    entry.runtime_data.async_set_updated_data(cleared_data)
    await hass.async_block_till_done()

    assert _state_time(hass, entity_id) == live_time


async def test_changed_slot_invalidates_preserved_live_eta(
    hass: HomeAssistant,
) -> None:
    """A rescheduled slot for the same order invalidates its old live ETA."""
    live_time, slot_start = _delivery_times()
    data = sample_api_data()
    data["next_order"] = [_order(7001, slot_start)]
    data["delivery_announcements"]["data"]["announcements"] = [
        _announcement(7001, live_time)
    ]

    entry, entity_id = await _setup_delivery_time(hass, data)

    changed_slot_start = slot_start + timedelta(hours=6)
    updated_data = copy.deepcopy(data)
    updated_data["next_order"] = [_order(7001, changed_slot_start)]
    updated_data["delivery_announcements"]["data"]["announcements"] = []
    entry.runtime_data.async_set_updated_data(updated_data)
    await hass.async_block_till_done()

    assert _state_time(hass, entity_id) == changed_slot_start


async def test_stale_live_eta_falls_back_to_slot(
    hass: HomeAssistant,
) -> None:
    """A live ETA is not preserved more than one hour after it passes."""
    live_time, slot_start = _delivery_times()
    data = sample_api_data()
    data["next_order"] = [_order(7001, slot_start)]
    data["delivery_announcements"]["data"]["announcements"] = [
        _announcement(7001, live_time)
    ]

    entry, entity_id = await _setup_delivery_time(hass, data)

    updated_data = copy.deepcopy(data)
    updated_data["delivery_announcements"]["data"]["announcements"] = []
    with patch(
        "custom_components.rohlikcz.sensor.dt_util.now",
        return_value=live_time + timedelta(hours=1, seconds=1),
    ):
        entry.runtime_data.async_set_updated_data(updated_data)
        await hass.async_block_till_done()

    assert _state_time(hass, entity_id) == slot_start


async def test_preserved_live_eta_survives_reload(
    hass: HomeAssistant,
) -> None:
    """Live ETA metadata is restored when the entry reloads after clearing."""
    live_time, slot_start = _delivery_times()
    data = sample_api_data()
    data["next_order"] = [_order(7001, slot_start)]
    data["delivery_announcements"]["data"]["announcements"] = [
        _announcement(7001, live_time)
    ]

    entry, entity_id = await _setup_delivery_time(hass, data)

    cleared_data = copy.deepcopy(data)
    cleared_data["delivery_announcements"]["data"]["announcements"] = []
    entry.runtime_data.async_set_updated_data(cleared_data)
    await hass.async_block_till_done()
    assert _state_time(hass, entity_id) == live_time

    with patch(
        "custom_components.rohlikcz.hub.RohlikAPI.get_data",
        new=AsyncMock(return_value=cleared_data),
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert _state_time(hass, entity_id) == live_time
