"""Tests for the shared delivery ETA: countdown, Last Mile and automatic refresh."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.rohlikcz.const import (
    CONF_AUTO_REFRESH_ETA,
    CONF_ETA_REFRESH_INTERVAL,
    CONF_LAST_MILE_LEAD,
    DOMAIN,
)

from fixtures_data import sample_api_data

PRAGUE = ZoneInfo("Europe/Prague")
NOW = datetime(2026, 11, 17, 10, 0, tzinfo=PRAGUE)
ETA = datetime(2026, 11, 17, 10, 30, tzinfo=PRAGUE)
SLOT_START = datetime(2026, 11, 17, 10, 0, tzinfo=PRAGUE)


def _order(order_id: int, since: datetime, hours: int = 2) -> dict:
    return {
        "id": order_id,
        "deliverySlot": {
            "since": since.isoformat(),
            "till": (since + timedelta(hours=hours)).isoformat(),
        },
    }


def _announcement(order_id: int, at: datetime) -> dict:
    return {
        "id": order_id,
        "title": "Delivery",
        "updatedAt": NOW.isoformat(),
        "content": f'Doručíme v <span style="color:#009B37">{at:%H:%M}</span>',
    }


def _data(eta: datetime | None = ETA, slot_start: datetime = SLOT_START) -> dict:
    data = sample_api_data()
    data["next_order"] = [_order(7001, slot_start)]
    data["delivery_announcements"]["data"]["announcements"] = (
        [_announcement(7001, eta)] if eta is not None else []
    )
    return data


def _with_announcements(data: dict, announcements: list) -> dict:
    updated = copy.deepcopy(data)
    updated["delivery_announcements"]["data"]["announcements"] = announcements
    return updated


async def _setup(
    hass: HomeAssistant, data: dict, options: dict | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"},
        options={CONF_AUTO_REFRESH_ETA: False, **(options or {})},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.rohlikcz.hub.RohlikAPI.get_data",
        new=AsyncMock(return_value=data),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    # Scheduled full refreshes return whatever the coordinator holds now.
    account = entry.runtime_data
    account._client.get_data = AsyncMock(side_effect=lambda: copy.deepcopy(account.data))
    return entry


def _entity_id(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"123456_{key}")
    assert entity_id is not None
    return entity_id


def _remaining(hass: HomeAssistant) -> str:
    return hass.states.get(_entity_id(hass, "sensor", "delivery_time_remaining")).state


def _last_mile(hass: HomeAssistant) -> str:
    return hass.states.get(_entity_id(hass, "binary_sensor", "last_mile")).state


async def _move_to(hass: HomeAssistant, freezer, when: datetime) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done()


async def _update(hass: HomeAssistant, entry: MockConfigEntry, data: dict) -> None:
    entry.runtime_data.async_set_updated_data(data)
    await hass.async_block_till_done()


# ---------------------------------------------------------------- remaining


async def test_remaining_counts_down_locally(hass: HomeAssistant, freezer) -> None:
    """The countdown updates every minute without new data, stops at 0."""
    freezer.move_to(NOW)
    entry = await _setup(hass, _data())
    poll = entry.runtime_data._client.delivery.get_announcements = AsyncMock()

    assert _remaining(hass) == "30"
    state = hass.states.get(_entity_id(hass, "sensor", "delivery_time_remaining"))
    assert state.attributes["source"] == "announcement"
    assert state.attributes["unit_of_measurement"] == "min"

    await _move_to(hass, freezer, NOW + timedelta(minutes=1))
    assert _remaining(hass) == "29"
    await _move_to(hass, freezer, NOW + timedelta(minutes=29, seconds=30))
    assert _remaining(hass) == "1"
    await _move_to(hass, freezer, ETA)
    assert _remaining(hass) == "0"
    # Late courier: stays at 0 ...
    await _move_to(hass, freezer, ETA + timedelta(minutes=20))
    assert _remaining(hass) == "0"
    # ... and gives up an hour after the ETA.
    await _move_to(hass, freezer, ETA + timedelta(minutes=61))
    assert _remaining(hass) == STATE_UNKNOWN
    poll.assert_not_called()


async def test_remaining_unknown_without_order(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    data = sample_api_data()
    data["next_order"] = []
    data["delivery_announcements"]["data"]["announcements"] = []
    await _setup(hass, data)

    assert _remaining(hass) == STATE_UNKNOWN
    assert _last_mile(hass) == STATE_OFF


async def test_remaining_far_away_does_not_tick(hass: HomeAssistant, freezer) -> None:
    """No per-minute state writes while the ETA is more than 90 minutes away."""
    freezer.move_to(NOW)
    far_slot = NOW + timedelta(hours=5)
    await _setup(hass, _data(eta=None, slot_start=far_slot))
    entity_id = _entity_id(hass, "sensor", "delivery_time_remaining")

    first = hass.states.get(entity_id)
    assert first.state == "300"
    assert first.attributes["source"] == "slot"

    await _move_to(hass, freezer, NOW + timedelta(minutes=5))
    assert hass.states.get(entity_id).last_updated == first.last_updated

    # Ticking starts 90 minutes before the ETA.
    await _move_to(hass, freezer, far_slot - timedelta(minutes=90))
    assert hass.states.get(entity_id).state == "90"
    await _move_to(hass, freezer, far_slot - timedelta(minutes=89))
    assert hass.states.get(entity_id).state == "89"


async def test_remaining_follows_new_eta(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    data = _data()
    entry = await _setup(hass, data)
    assert _remaining(hass) == "30"

    later = ETA + timedelta(minutes=15)
    await _update(hass, entry, _with_announcements(data, [_announcement(7001, later)]))
    assert _remaining(hass) == "45"
    await _move_to(hass, freezer, NOW + timedelta(minutes=1))
    assert _remaining(hass) == "44"


# ---------------------------------------------------------------- last mile


async def test_last_mile_turns_on_before_eta_and_off_after_delivery(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(NOW)
    data = _data()
    entry = await _setup(hass, data)
    entity_id = _entity_id(hass, "binary_sensor", "last_mile")

    assert _last_mile(hass) == STATE_OFF
    assert hass.states.get(entity_id).attributes["lead_minutes"] == 5

    await _move_to(hass, freezer, ETA - timedelta(minutes=5, seconds=1))
    assert _last_mile(hass) == STATE_OFF
    await _move_to(hass, freezer, ETA - timedelta(minutes=5))
    assert _last_mile(hass) == STATE_ON

    # Rohlík clears the announcement before the courier arrives: stays on.
    await _update(hass, entry, _with_announcements(data, []))
    assert _last_mile(hass) == STATE_ON

    # Delivered: the order is no longer upcoming.
    delivered = _with_announcements(data, [])
    delivered["next_order"] = []
    await _update(hass, entry, delivered)
    assert _last_mile(hass) == STATE_OFF


async def test_last_mile_gives_up_after_an_hour(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(ETA)
    await _setup(hass, _data())
    assert _last_mile(hass) == STATE_ON

    await _move_to(hass, freezer, ETA + timedelta(minutes=61))
    assert _last_mile(hass) == STATE_OFF


async def test_last_mile_ignores_slot_fallback(hass: HomeAssistant, freezer) -> None:
    """The booked slot alone never turns Last Mile on."""
    freezer.move_to(SLOT_START - timedelta(minutes=10))
    await _setup(hass, _data(eta=None))

    await _move_to(hass, freezer, SLOT_START)
    assert _remaining(hass) == "0"
    assert _last_mile(hass) == STATE_OFF


async def test_last_mile_follows_later_eta(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(ETA - timedelta(minutes=3))
    data = _data()
    entry = await _setup(hass, data)
    assert _last_mile(hass) == STATE_ON

    later = ETA + timedelta(minutes=20)
    await _update(hass, entry, _with_announcements(data, [_announcement(7001, later)]))
    assert _last_mile(hass) == STATE_OFF

    await _move_to(hass, freezer, later - timedelta(minutes=5))
    assert _last_mile(hass) == STATE_ON


async def test_last_mile_lead_option(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(ETA - timedelta(minutes=9))
    await _setup(hass, _data(), {CONF_LAST_MILE_LEAD: 10})

    assert _last_mile(hass) == STATE_ON
    state = hass.states.get(_entity_id(hass, "binary_sensor", "last_mile"))
    assert state.attributes["lead_minutes"] == 10


async def test_last_mile_stays_available_through_failed_refresh(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(ETA - timedelta(minutes=2))
    entry = await _setup(hass, _data())
    assert _last_mile(hass) == STATE_ON

    entry.runtime_data.async_set_update_error(Exception("boom"))
    await hass.async_block_till_done()
    assert _last_mile(hass) == STATE_ON
    assert _remaining(hass) == "2"


async def test_restart_inside_last_mile_window_does_not_flap(
    hass: HomeAssistant, freezer
) -> None:
    """After a restart with the announcement gone, Last Mile comes back on."""
    freezer.move_to(ETA - timedelta(minutes=2))
    data = _data()
    entry = await _setup(hass, data)
    assert _last_mile(hass) == STATE_ON

    cleared = _with_announcements(data, [])
    await _update(hass, entry, cleared)
    entity_id = _entity_id(hass, "binary_sensor", "last_mile")

    seen: list[str] = []
    hass.bus.async_listen(
        "state_changed",
        lambda event: event.data["entity_id"] == entity_id
        and event.data["new_state"] is not None
        and seen.append(event.data["new_state"].state),
    )
    with patch(
        "custom_components.rohlikcz.hub.RohlikAPI.get_data",
        new=AsyncMock(return_value=cleared),
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert _last_mile(hass) == STATE_ON
    assert STATE_OFF not in seen
    assert _remaining(hass) == "2"


async def test_stale_live_eta_expires_without_new_data(
    hass: HomeAssistant, freezer
) -> None:
    """A kept ETA is dropped an hour after it passes, even with no refresh."""
    freezer.move_to(NOW)
    data = _data()
    entry = await _setup(hass, data)
    await _update(hass, entry, _with_announcements(data, []))
    delivery_time = _entity_id(hass, "sensor", "delivery_time")
    assert dt_util.parse_datetime(hass.states.get(delivery_time).state) == ETA

    await _move_to(hass, freezer, ETA + timedelta(hours=1, seconds=2))
    assert dt_util.parse_datetime(hass.states.get(delivery_time).state) == SLOT_START
    assert entry.runtime_data.eta.source == "slot"


async def test_failed_announcements_request_does_not_break(
    hass: HomeAssistant, freezer
) -> None:
    """The API client stores None when the announcements request fails."""
    freezer.move_to(NOW)
    data = _data()
    data["delivery_announcements"] = None
    await _setup(hass, data)

    assert dt_util.parse_datetime(
        hass.states.get(_entity_id(hass, "sensor", "delivery_time")).state
    ) == SLOT_START
    assert _last_mile(hass) == STATE_OFF


# ------------------------------------------------------------ auto refresh


async def test_auto_refresh_polls_only_near_delivery(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(ETA - timedelta(minutes=45))
    data = _data()
    entry = await _setup(
        hass, data, {CONF_AUTO_REFRESH_ETA: True, CONF_ETA_REFRESH_INTERVAL: 60}
    )
    later = ETA + timedelta(minutes=5)
    poll = entry.runtime_data._client.delivery.get_announcements = AsyncMock(
        return_value=copy.deepcopy(data["delivery_announcements"])
    )

    # Before the window (ETA - 30 min) nothing is requested.
    await _move_to(hass, freezer, ETA - timedelta(minutes=31))
    poll.assert_not_called()

    # The window opens: one request immediately, then one per interval.
    await _move_to(hass, freezer, ETA - timedelta(minutes=30))
    assert poll.call_count == 1
    await _move_to(hass, freezer, ETA - timedelta(minutes=29, seconds=30))
    assert poll.call_count == 1
    await _move_to(hass, freezer, ETA - timedelta(minutes=29))
    assert poll.call_count == 2

    # A new ETA arrives through the poll and moves the countdown.
    moved = _with_announcements(data, [_announcement(7001, later)])
    poll.return_value = copy.deepcopy(moved["delivery_announcements"])
    await _move_to(hass, freezer, ETA - timedelta(minutes=28))
    assert poll.call_count == 3
    assert _remaining(hass) == "33"

    # Past the end of the booked slot + 10 min, polling stops.
    end = SLOT_START + timedelta(hours=2, minutes=10)
    for minute in range(1, 200):
        when = ETA - timedelta(minutes=28) + timedelta(minutes=minute)
        if when > end:
            break
        await _move_to(hass, freezer, when)
    # The poll armed just before the end still runs once.
    await _move_to(hass, freezer, end + timedelta(minutes=5))
    calls = poll.call_count
    await _move_to(hass, freezer, end + timedelta(minutes=10))
    await _move_to(hass, freezer, end + timedelta(minutes=20))
    assert poll.call_count == calls


async def test_auto_refresh_interval_option(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(ETA - timedelta(minutes=10))
    data = _data()
    entry = await _setup(
        hass, data, {CONF_AUTO_REFRESH_ETA: True, CONF_ETA_REFRESH_INTERVAL: 120}
    )
    poll = entry.runtime_data._client.delivery.get_announcements = AsyncMock(
        return_value=copy.deepcopy(data["delivery_announcements"])
    )

    await _move_to(hass, freezer, ETA - timedelta(minutes=9))
    poll.assert_not_called()
    await _move_to(hass, freezer, ETA - timedelta(minutes=8))
    assert poll.call_count == 1


async def test_auto_refresh_disabled(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(ETA - timedelta(minutes=10))
    entry = await _setup(hass, _data(), {CONF_AUTO_REFRESH_ETA: False})
    poll = entry.runtime_data._client.delivery.get_announcements = AsyncMock()

    for minute in range(1, 6):
        await _move_to(hass, freezer, ETA - timedelta(minutes=10 - minute))
    poll.assert_not_called()


async def test_auto_refresh_survives_errors(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(ETA - timedelta(minutes=10))
    entry = await _setup(
        hass, _data(), {CONF_AUTO_REFRESH_ETA: True, CONF_ETA_REFRESH_INTERVAL: 60}
    )
    poll = entry.runtime_data._client.delivery.get_announcements = AsyncMock(
        side_effect=RuntimeError("boom")
    )

    await _move_to(hass, freezer, ETA - timedelta(minutes=9))
    await _move_to(hass, freezer, ETA - timedelta(minutes=8))
    assert poll.call_count == 2


async def test_unload_stops_timers_and_saves_eta(
    hass: HomeAssistant, freezer, hass_storage
) -> None:
    freezer.move_to(ETA - timedelta(minutes=10))
    entry = await _setup(
        hass, _data(), {CONF_AUTO_REFRESH_ETA: True, CONF_ETA_REFRESH_INTERVAL: 60}
    )
    poll = entry.runtime_data._client.delivery.get_announcements = AsyncMock()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await _move_to(hass, freezer, ETA - timedelta(minutes=5))
    poll.assert_not_called()

    stored = hass_storage[f"{DOMAIN}_{entry.entry_id}_eta"]["data"]
    assert dt_util.parse_datetime(stored["last_live_value"]) == ETA
    assert stored["last_live_order_id"] == "7001"


async def test_options_flow_saves_delivery_settings(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _data())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    with patch(
        "custom_components.rohlikcz.hub.RohlikAPI.get_data",
        new=AsyncMock(return_value=_data()),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_LAST_MILE_LEAD: 8.0,
                CONF_AUTO_REFRESH_ETA: True,
                CONF_ETA_REFRESH_INTERVAL: 90.0,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_LAST_MILE_LEAD] == 8
    assert entry.options[CONF_AUTO_REFRESH_ETA] is True
    assert entry.options[CONF_ETA_REFRESH_INTERVAL] == 90
    assert entry.runtime_data.last_mile_lead == timedelta(minutes=8)


async def test_remaining_across_dst_change(hass: HomeAssistant, freezer) -> None:
    """The countdown counts real minutes when clocks go back (25.10.2026)."""
    # 02:50 summer time is 00:50 UTC; 02:30 winter time (after the clocks go
    # back at 03:00) is 01:30 UTC, i.e. 40 minutes later, not 20 earlier.
    freezer.move_to(datetime(2026, 10, 25, 0, 50, tzinfo=ZoneInfo("UTC")))
    data = sample_api_data()
    data["next_order"] = [{
        "id": 7001,
        "deliverySlot": {
            "since": "2026-10-25T02:30:00+01:00",
            "till": "2026-10-25T03:30:00+01:00",
        },
    }]
    data["delivery_announcements"]["data"]["announcements"] = []
    await _setup(hass, data)

    assert _remaining(hass) == "40"
