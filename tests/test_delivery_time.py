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
from custom_components.rohlikcz.sensor import announcement_slot

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


def _minutes_announcement(order_id: int, minutes: int) -> dict:
    return {
        "id": order_id,
        "title": "Delivery",
        "updatedAt": datetime.now(ZoneInfo("Europe/Prague")).isoformat(),
        "content": f"Váš nákup doručíme přibližně za {minutes} minuty.",
    }


async def test_short_minutes_announcement_sets_eta(
    hass: HomeAssistant,
) -> None:
    """The short "za X minut" announcement replaces the earlier clock-time ETA."""
    live_time, slot_start = _delivery_times()
    data = sample_api_data()
    data["next_order"] = [_order(7001, slot_start)]
    data["delivery_announcements"]["data"]["announcements"] = [
        _announcement(7001, live_time)
    ]

    entry, entity_id = await _setup_delivery_time(hass, data)
    assert _state_time(hass, entity_id) == live_time

    updated_data = copy.deepcopy(data)
    updated_data["delivery_announcements"]["data"]["announcements"] = [
        _minutes_announcement(7001, 2)
    ]
    before = dt_util.now()
    entry.runtime_data.async_set_updated_data(updated_data)
    await hass.async_block_till_done()
    after = dt_util.now()

    eta = _state_time(hass, entity_id)
    assert eta is not None
    # The state is serialized with whole seconds.
    assert (
        before.replace(microsecond=0) + timedelta(minutes=2)
        <= eta
        <= after + timedelta(minutes=2)
    )


async def test_unchanged_minutes_announcement_does_not_drift(
    hass: HomeAssistant, freezer
) -> None:
    """Re-polling the same relative announcement keeps the original ETA."""
    _, slot_start = _delivery_times()
    data = sample_api_data()
    data["next_order"] = [_order(7001, slot_start)]
    data["delivery_announcements"]["data"]["announcements"] = [
        _minutes_announcement(7001, 2)
    ]

    entry, entity_id = await _setup_delivery_time(hass, data)
    first_eta = _state_time(hass, entity_id)
    assert first_eta is not None

    freezer.tick(timedelta(minutes=1))
    entry.runtime_data.async_set_updated_data(copy.deepcopy(data))
    await hass.async_block_till_done()
    assert _state_time(hass, entity_id) == first_eta

    # A new text restarts the count from when it arrived.
    changed_data = copy.deepcopy(data)
    changed_data["delivery_announcements"]["data"]["announcements"] = [
        _minutes_announcement(7001, 3)
    ]
    entry.runtime_data.async_set_updated_data(changed_data)
    await hass.async_block_till_done()
    assert _state_time(hass, entity_id) == (
        dt_util.now() + timedelta(minutes=3)
    ).replace(microsecond=0)


async def test_repeated_announcement_after_clearing_restarts_count(
    hass: HomeAssistant, freezer
) -> None:
    """The same text arriving again after the announcement cleared is new."""
    _, slot_start = _delivery_times()
    data = sample_api_data()
    data["next_order"] = [_order(7001, slot_start)]
    data["delivery_announcements"]["data"]["announcements"] = [
        _minutes_announcement(7001, 2)
    ]

    entry, entity_id = await _setup_delivery_time(hass, data)

    cleared_data = copy.deepcopy(data)
    cleared_data["delivery_announcements"]["data"]["announcements"] = []
    entry.runtime_data.async_set_updated_data(cleared_data)
    await hass.async_block_till_done()

    freezer.tick(timedelta(minutes=30))
    entry.runtime_data.async_set_updated_data(copy.deepcopy(data))
    await hass.async_block_till_done()
    assert _state_time(hass, entity_id) == (
        dt_util.now() + timedelta(minutes=2)
    ).replace(microsecond=0)


async def test_overdue_minutes_announcement_is_due_now(
    hass: HomeAssistant, freezer
) -> None:
    """An unchanged countdown that has run out reports now, not a past ETA."""
    _, slot_start = _delivery_times()
    data = sample_api_data()
    data["next_order"] = [_order(7001, slot_start)]
    data["delivery_announcements"]["data"]["announcements"] = [
        _minutes_announcement(7001, 2)
    ]

    entry, entity_id = await _setup_delivery_time(hass, data)

    freezer.tick(timedelta(minutes=5))
    entry.runtime_data.async_set_updated_data(copy.deepcopy(data))
    await hass.async_block_till_done()
    assert _state_time(hass, entity_id) == dt_util.now().replace(microsecond=0)


async def test_time_only_announcement_lands_on_slot_day(
    hass: HomeAssistant, freezer
) -> None:
    """Issue #106: a time without a date is placed on the booked slot's day.

    Received on the evening of 30.10. for a slot on 1.11., placing 09:15 after
    the receipt time would report 31.10., a day early. The deprecated attribute
    on Delivery Info must agree with the sensor.
    """
    prague = ZoneInfo("Europe/Prague")
    freezer.move_to(datetime(2026, 10, 30, 19, 16, tzinfo=prague))
    slot_start = datetime(2026, 11, 1, 9, 0, tzinfo=prague)
    data = sample_api_data()
    data["next_order"] = [_order(7001, slot_start)]
    announcement = _announcement(7001, slot_start)
    announcement["content"] = "Váš nákup přivezeme v <span>09:15</span>."
    data["delivery_announcements"]["data"]["announcements"] = [announcement]

    _, entity_id = await _setup_delivery_time(hass, data)

    expected_eta = datetime(2026, 11, 1, 9, 15, tzinfo=prague)
    assert _state_time(hass, entity_id) == expected_eta

    info_entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, "123456_delivery_info"
    )
    info_state = hass.states.get(info_entity_id)
    assert info_state is not None
    assert info_state.attributes["Delivery time (deprecated, use new entity)"] == expected_eta


async def test_time_only_announcement_with_concurrent_orders(
    hass: HomeAssistant, freezer
) -> None:
    """With two orders, the announcement's own order supplies the slot's day."""
    prague = ZoneInfo("Europe/Prague")
    freezer.move_to(datetime(2026, 10, 30, 19, 16, tzinfo=prague))
    slot_start = datetime(2026, 11, 1, 9, 0, tzinfo=prague)
    data = sample_api_data()
    data["next_order"] = [
        _order(7002, slot_start + timedelta(days=3)),
        _order(7001, slot_start),
    ]
    announcement = _announcement(7001, slot_start)
    announcement["content"] = "Váš nákup přivezeme v <span>09:15</span>."
    data["delivery_announcements"]["data"]["announcements"] = [announcement]

    _, entity_id = await _setup_delivery_time(hass, data)

    assert _state_time(hass, entity_id) == datetime(2026, 11, 1, 9, 15, tzinfo=prague)


def test_announcement_slot_matches_by_id() -> None:
    """The slot is that of the order with the announcement's ID, wherever it is."""
    prague = ZoneInfo("Europe/Prague")
    first = datetime(2026, 11, 1, 9, 0, tzinfo=prague)
    later = datetime(2026, 11, 4, 18, 0, tzinfo=prague)
    orders = [_order(7001, first), _order(7002, later)]

    assert announcement_slot({"id": 7002}, orders) == (later, later + timedelta(hours=1))
    assert announcement_slot({"id": "7001"}, orders) == (first, first + timedelta(hours=1))


def test_announcement_slot_without_matching_order() -> None:
    """No order with the announcement's ID means no slot, even with one order.

    Borrowing the only order's slot would move the ETA onto that order's day.
    """
    slot_start = datetime(2026, 11, 2, 9, 0, tzinfo=ZoneInfo("Europe/Prague"))

    assert announcement_slot({"id": 7009}, [_order(7001, slot_start)]) == (None, None)
    assert announcement_slot({"id": 7001}, None) == (None, None)
    assert announcement_slot({"id": 7001}, [{"id": 7001}]) == (None, None)
