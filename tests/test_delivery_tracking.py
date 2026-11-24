"""Tests for live delivery tracking: fetching, ETA source, entities and privacy."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from rohlik_api import APIRequestFailedError, DeliveryTracking

from custom_components.rohlikcz.const import (
    CONF_AUTO_REFRESH_ETA,
    CONF_ETA_REFRESH_INTERVAL,
    DOMAIN,
)
from custom_components.rohlikcz.diagnostics import async_get_config_entry_diagnostics
from custom_components.rohlikcz.utils import strip_html_and_phones

from fixtures_data import sample_api_data

PRAGUE = ZoneInfo("Europe/Prague")
NOW = datetime(2026, 11, 17, 10, 0, tzinfo=PRAGUE)
SLOT_START = datetime(2026, 11, 17, 11, 0, tzinfo=PRAGUE)
ORDER_ID = 7001
COURIER = "Courier"
PHONE = "+420000000000"


# ------------------------------------------------------------------- helpers


def _order(order_id: int = ORDER_ID, since: datetime = SLOT_START) -> dict:
    return {
        "id": order_id,
        "deliverySlot": {
            "since": since.isoformat(),
            "till": (since + timedelta(minutes=15)).isoformat(),
        },
    }


def _data(orders: list | None = None, announcements: list | None = None) -> dict:
    data = sample_api_data()
    data["next_order"] = [_order()] if orders is None else orders
    data["delivery_announcements"]["data"]["announcements"] = announcements or []
    return data


def _tracking(
    status: str | None = "IN_QUEUE",
    eta: datetime | None = SLOT_START - timedelta(minutes=10),
    stops: int | None = 3,
    order_id: int = ORDER_ID,
    early: bool = True,
    slot_since: datetime = SLOT_START,
    position: tuple[float, float] = (50.001, 14.001),
    stale: bool | None = False,
    recorded_ago: timedelta | None = timedelta(seconds=30),
) -> DeliveryTracking:
    """Tracking built from placeholder values, like the library's fixtures."""
    utc = ZoneInfo("UTC")
    progress = None
    if status is not None:
        progress = {
            "generatedAt": dt_util.utcnow().isoformat(),
            "deliveryStatus": status,
            "stopsBefore": stops,
            "estimatedArrival": eta.astimezone(utc).isoformat() if eta else None,
            "courierLocationStale": stale,
            "courierPosition": (
                {
                    "latitude": position[0],
                    "longitude": position[1],
                    "recordedAt": (
                        (dt_util.utcnow() - recorded_ago).isoformat()
                        if recorded_ago is not None
                        else None
                    ),
                }
                if status in ("ON_THE_WAY", "ARRIVED", "HANDING_OVER")
                else None
            ),
        }
    return DeliveryTracking.from_api(
        {
            "orderId": order_id,
            "deliverySlot": {
                "since": slot_since.astimezone(utc).isoformat(),
                "till": (slot_since + timedelta(minutes=15)).astimezone(utc).isoformat(),
                "type": "ON_TIME",
            },
            "destination": {"latitude": 50.0, "longitude": 14.0},
            "courier": {"displayName": COURIER, "phoneNumber": PHONE, "photoUrl": None},
            "earlyDelivery": {
                "available": True,
                "requested": early,
                "allowedFrom": (
                    (slot_since - timedelta(hours=1)).astimezone(utc).isoformat()
                    if early
                    else None
                ),
            },
            "progress": progress,
        }
    )


async def _setup(
    hass: HomeAssistant,
    data: dict,
    tracking: DeliveryTracking | None | Exception = None,
    options: dict | None = None,
) -> tuple[MockConfigEntry, AsyncMock]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"},
        options={CONF_AUTO_REFRESH_ETA: False, **(options or {})},
    )
    entry.add_to_hass(hass)
    fetch = AsyncMock(
        side_effect=tracking if isinstance(tracking, Exception) else None,
        return_value=None if isinstance(tracking, Exception) else tracking,
    )
    with patch(
        "custom_components.rohlikcz.hub.RohlikAPI.get_data",
        new=AsyncMock(return_value=data),
    ), patch(
        "rohlik_api.services.orders.OrderService.get_delivery_tracking", new=fetch
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    account = entry.runtime_data
    account._client.get_data = AsyncMock(side_effect=lambda: copy.deepcopy(account.data))
    account._client.orders.get_delivery_tracking = fetch
    return entry, fetch


def _state(hass: HomeAssistant, platform: str, key: str):
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"123456_{key}")
    assert entity_id is not None, key
    return hass.states.get(entity_id)


def _status(hass: HomeAssistant) -> str:
    return _state(hass, "sensor", "delivery_status").state


async def _move_to(hass: HomeAssistant, freezer, when: datetime) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done()


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry, data: dict | None = None) -> None:
    """Run a full coordinator refresh returning ``data`` (or the current data)."""
    account = entry.runtime_data
    if data is not None:
        account._client.get_data = AsyncMock(return_value=copy.deepcopy(data))
    await account.async_refresh()
    await hass.async_block_till_done()


# ------------------------------------------------------------------ fetching


async def test_first_refresh_fetches_tracking_from_fresh_data(hass: HomeAssistant, freezer) -> None:
    """On setup self.data is still None: the fresh data must be used."""
    freezer.move_to(NOW)
    _entry, fetch = await _setup(hass, _data(), _tracking("ON_THE_WAY", stops=0))
    assert fetch.await_count == 1
    assert fetch.await_args.args[0] == ORDER_ID
    assert fetch.await_args.kwargs == {"raise_on_error": True}
    assert _status(hass) == "on_the_way"
    assert _state(hass, "sensor", "stops_before").state == "0"


async def test_no_request_for_an_order_beyond_the_lookahead(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    data = _data([_order(since=NOW + timedelta(hours=4, minutes=1))])
    _entry, fetch = await _setup(hass, data, _tracking())
    fetch.assert_not_called()
    assert _status(hass) == STATE_UNKNOWN


async def test_no_request_without_an_upcoming_order(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    _entry, fetch = await _setup(hass, _data(orders=[]), _tracking())
    fetch.assert_not_called()
    assert _status(hass) == STATE_UNKNOWN


async def test_not_found_is_cached_per_order(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    entry, fetch = await _setup(hass, _data(), None)
    assert fetch.await_count == 1
    await _refresh(hass, entry)
    await _refresh(hass, entry)
    assert fetch.await_count == 1
    assert _status(hass) == STATE_UNKNOWN


async def test_failure_keeps_tracking_until_max_age(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    entry, fetch = await _setup(hass, _data(), _tracking())
    fetch.side_effect = APIRequestFailedError("boom")

    await _move_to(hass, freezer, NOW + timedelta(minutes=10))
    await _refresh(hass, entry)
    assert _status(hass) == "in_queue"

    await _move_to(hass, freezer, NOW + timedelta(minutes=16))
    await _refresh(hass, entry)
    assert _status(hass) == STATE_UNKNOWN


async def test_failed_upcoming_request_keeps_state(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    entry, fetch = await _setup(hass, _data(), _tracking())
    broken = _data()
    broken["next_order"] = None
    await _refresh(hass, entry, broken)
    assert fetch.await_count == 1
    assert _status(hass) == "in_queue"


async def test_seconds_of_eta_jitter_are_not_a_change(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    entry, fetch = await _setup(hass, _data(), _tracking(eta=SLOT_START - timedelta(minutes=10)))
    account = entry.runtime_data
    fetch.return_value = _tracking(eta=SLOT_START - timedelta(minutes=10, seconds=-20))
    changed, _ = await account._async_refresh_tracking(account.data)
    assert changed is False
    fetch.return_value = _tracking(eta=SLOT_START - timedelta(minutes=8))
    changed, _ = await account._async_refresh_tracking(account.data)
    assert changed is True


# --------------------------------------------------------------- ETA source


async def test_tracking_beats_the_announcement(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    announcement = {
        "id": ORDER_ID,
        "title": "Delivery",
        "updatedAt": NOW.isoformat(),
        "content": 'Doručíme v <span>11:00</span>',
    }
    eta = SLOT_START - timedelta(minutes=12)
    await _setup(hass, _data(announcements=[announcement]), _tracking(eta=eta, early=True))
    state = _state(hass, "sensor", "delivery_time")
    assert dt_util.parse_datetime(state.state) == eta
    remaining = _state(hass, "sensor", "delivery_time_remaining")
    assert remaining.attributes["source"] == "tracking"


async def test_queued_eta_held_at_slot_start_without_early_delivery(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(NOW)
    await _setup(hass, _data(), _tracking(eta=SLOT_START - timedelta(minutes=12), early=False))
    state = _state(hass, "sensor", "delivery_time")
    assert dt_util.parse_datetime(state.state) == SLOT_START


async def test_tracking_without_estimate_falls_back_to_slot(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    await _setup(hass, _data(), _tracking(eta=None))
    remaining = _state(hass, "sensor", "delivery_time_remaining")
    assert remaining.attributes["source"] == "slot"
    assert _status(hass) == "in_queue"


async def test_tracking_for_another_order_is_ignored_for_eta(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    await _setup(hass, _data(), _tracking(order_id=9999))
    remaining = _state(hass, "sensor", "delivery_time_remaining")
    assert remaining.attributes["source"] == "slot"


# ------------------------------------------------------------------ entities


async def test_unknown_status_shows_unknown(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    await _setup(hass, _data(), _tracking("DELAYED"))
    assert _status(hass) == STATE_UNKNOWN


async def test_delivery_status_options_and_attributes(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    await _setup(hass, _data(), _tracking("IN_QUEUE"))
    state = _state(hass, "sensor", "delivery_status")
    assert state.attributes["options"] == [
        "in_queue", "on_the_way", "arrived", "handing_over", "delivered",
    ]
    assert state.attributes["order_id"] == ORDER_ID
    assert COURIER not in str(state.attributes)


async def test_last_mile_does_not_flap_when_the_eta_moves(hass: HomeAssistant, freezer) -> None:
    """Replays the observed jump: on the way, ETA 12:43 -> 12:46 with a 5-min lead."""
    eta = NOW + timedelta(minutes=5)
    freezer.move_to(NOW)
    entry, fetch = await _setup(hass, _data(), _tracking("ON_THE_WAY", eta=eta, stops=0))
    assert _state(hass, "binary_sensor", "last_mile").state == STATE_ON

    fetch.return_value = _tracking("ON_THE_WAY", eta=eta + timedelta(minutes=3), stops=0)
    await _move_to(hass, freezer, NOW + timedelta(minutes=1))
    await _refresh(hass, entry)
    assert _state(hass, "binary_sensor", "last_mile").state == STATE_ON

    fetch.return_value = _tracking("DELIVERED", eta=eta + timedelta(minutes=3), stops=None)
    await _move_to(hass, freezer, NOW + timedelta(minutes=8))
    await _refresh(hass, entry)
    assert _state(hass, "binary_sensor", "last_mile").state == STATE_OFF


async def test_last_mile_turns_on_when_the_courier_arrives(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    # ETA well ahead (outside the lead), but the courier is already at the door.
    await _setup(hass, _data(), _tracking("ARRIVED", eta=NOW + timedelta(minutes=30), stops=0))
    assert _state(hass, "binary_sensor", "last_mile").state == STATE_ON
    assert _state(hass, "binary_sensor", "courier_at_door").state == STATE_ON


async def test_courier_at_door_latches_until_delivered(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    entry, fetch = await _setup(hass, _data(), _tracking("ON_THE_WAY", stops=0))
    assert _state(hass, "binary_sensor", "courier_at_door").state == STATE_OFF

    fetch.return_value = _tracking("ARRIVED", stops=0)
    await _refresh(hass, entry)
    assert _state(hass, "binary_sensor", "courier_at_door").state == STATE_ON

    fetch.return_value = _tracking("HANDING_OVER", stops=0)
    await _refresh(hass, entry)
    assert _state(hass, "binary_sensor", "courier_at_door").state == STATE_ON

    # A failed refresh must not make it unavailable (it would re-fire automations).
    entry.runtime_data.async_set_update_error(Exception("boom"))
    await hass.async_block_till_done()
    assert _state(hass, "binary_sensor", "courier_at_door").state == STATE_ON
    assert _status(hass) == "handing_over"

    fetch.return_value = _tracking("DELIVERED", stops=None)
    await _refresh(hass, entry)
    assert _state(hass, "binary_sensor", "courier_at_door").state == STATE_OFF
    assert _status(hass) == "delivered"


# --------------------------------------------------- order leaves the list


async def test_final_fetch_catches_delivered_and_hold_expires(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    entry, fetch = await _setup(hass, _data(), _tracking("HANDING_OVER", stops=0))
    fetch.return_value = _tracking("DELIVERED", stops=None)
    await _refresh(hass, entry, _data(orders=[]))
    assert fetch.await_args.args[0] == ORDER_ID
    assert _status(hass) == "delivered"
    assert _state(hass, "binary_sensor", "last_mile").state == STATE_OFF

    calls = fetch.await_count
    await _move_to(hass, freezer, NOW + timedelta(minutes=10))
    await _refresh(hass, entry)
    assert fetch.await_count == calls  # delivered: no more fetches
    assert _status(hass) == "delivered"

    await _move_to(hass, freezer, NOW + timedelta(minutes=31))
    assert _status(hass) == STATE_UNKNOWN


async def test_order_that_leaves_without_delivered_expires(hass: HomeAssistant, freezer) -> None:
    """Cancelled or a failing final fetch: at most 3 attempts, then it expires."""
    freezer.move_to(NOW)
    entry, fetch = await _setup(hass, _data(), _tracking("ARRIVED", stops=0))
    fetch.side_effect = APIRequestFailedError("boom")
    gone = _data(orders=[])
    for minutes in (1, 2, 3, 4, 5):
        await _move_to(hass, freezer, NOW + timedelta(minutes=minutes))
        await _refresh(hass, entry, gone)
    assert fetch.await_count == 1 + 3
    await _move_to(hass, freezer, NOW + timedelta(minutes=40))
    await _refresh(hass, entry, gone)
    assert _status(hass) == STATE_UNKNOWN
    assert _state(hass, "binary_sensor", "courier_at_door").state == STATE_OFF


async def test_next_order_ends_the_delivered_hold(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    entry, fetch = await _setup(hass, _data(), _tracking("HANDING_OVER", stops=0))
    fetch.return_value = _tracking("DELIVERED", stops=None)
    await _refresh(hass, entry, _data(orders=[]))
    assert _status(hass) == "delivered"

    fetch.return_value = _tracking("IN_QUEUE", order_id=7002, stops=4)
    nxt = _data(orders=[_order(7002, NOW + timedelta(hours=1))])
    # "delivered" stays visible for at least 5 minutes ...
    await _refresh(hass, entry, nxt)
    assert _status(hass) == "delivered"
    # ... then the next order takes over, before the 30-minute hold ends.
    await _move_to(hass, freezer, NOW + timedelta(minutes=6))
    await _refresh(hass, entry, nxt)
    assert _status(hass) == "in_queue"
    assert _state(hass, "sensor", "delivery_status").attributes["order_id"] == 7002


# ---------------------------------------------------------------- polling


async def test_late_stage_polls_past_eta_and_skips_announcements(
    hass: HomeAssistant, freezer
) -> None:
    eta = NOW + timedelta(minutes=5)
    freezer.move_to(NOW)
    entry, fetch = await _setup(
        hass,
        _data(),
        _tracking("ON_THE_WAY", eta=eta, stops=0),
        {CONF_AUTO_REFRESH_ETA: True, CONF_ETA_REFRESH_INTERVAL: 300},
    )
    account = entry.runtime_data
    announcements = account._client.delivery.get_announcements = AsyncMock(return_value=None)
    refresh = account.async_request_refresh = AsyncMock()

    # Courier close: at most every 60 s even with a 300-s interval, and well
    # past ETA + 10 min; announcements are skipped while tracking has an ETA.
    calls = fetch.await_count
    when = NOW
    for _ in range(20):
        when += timedelta(seconds=61)
        await _move_to(hass, freezer, when)
    assert when > eta + timedelta(minutes=10)
    assert fetch.await_count - calls >= 18
    announcements.assert_not_called()

    # Delivered: a full refresh is requested and polling stops.
    fetch.return_value = _tracking("DELIVERED", eta=eta, stops=None)
    when += timedelta(seconds=61)
    await _move_to(hass, freezer, when)
    refresh.assert_awaited()
    calls = fetch.await_count
    for _ in range(3):
        when += timedelta(seconds=61)
        await _move_to(hass, freezer, when)
    assert fetch.await_count == calls


async def test_poll_falls_back_to_announcements_without_tracking(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(SLOT_START - timedelta(minutes=20))
    entry, _fetch = await _setup(
        hass, _data(), None, {CONF_AUTO_REFRESH_ETA: True, CONF_ETA_REFRESH_INTERVAL: 60}
    )
    announcements = entry.runtime_data._client.delivery.get_announcements = AsyncMock(
        return_value=None
    )
    await _move_to(hass, freezer, SLOT_START - timedelta(minutes=19))
    announcements.assert_awaited()


# ------------------------------------------------------------------ privacy


def test_phone_numbers_are_stripped_from_announcement_text() -> None:
    text = (
        f"<span>{COURIER}</span> už Vám veze celkem <span>8</span> tašek. A kdyby něco, "
        f'je na čísle <a href="tel:{PHONE}"><strong>{PHONE}</strong></a>.'
    )
    assert strip_html_and_phones(text) == f"{COURIER} už Vám veze celkem 8 tašek."
    assert strip_html_and_phones("Volejte 777 123 456. Díky!") == "Díky!"
    # Long digit runs are dropped too (privacy wins over keeping an order number).
    assert strip_html_and_phones("Objednávka 1000000001 je na cestě. Díky!") == "Díky!"


async def test_delivery_info_attribute_has_no_phone(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    announcement = {
        "id": ORDER_ID,
        "title": "Delivery",
        "updatedAt": NOW.isoformat(),
        "content": "Doručíme za <span>8</span> minut.",
        "additionalContent": f"Veze <span>8</span> tašek. Volejte <strong>{PHONE}</strong>.",
    }
    await _setup(hass, _data(announcements=[announcement]))
    state = _state(hass, "sensor", "delivery_info")
    assert PHONE not in str(state.attributes)
    assert "8 tašek" in state.attributes["Additional Content"]


async def test_diagnostics_hide_courier_and_location(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    announcement = {
        "id": ORDER_ID,
        "title": "Delivery",
        "updatedAt": NOW.isoformat(),
        "content": "Doručíme za <span>8</span> minut.",
        "additionalContent": f"{COURIER} veze 8 tašek. Volejte {PHONE}.",
    }
    entry, _fetch = await _setup(
        hass, _data(announcements=[announcement]), _tracking("ON_THE_WAY", stops=0)
    )
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    assert COURIER not in text
    assert PHONE not in text
    assert "50.001" not in text and "14.001" not in text
    tracking = diag["coordinator"]["delivery_tracking"]
    assert tracking["status"] == "ON_THE_WAY"
    assert tracking["order_id"] == ORDER_ID
    assert tracking["valid"] is True


# --------------------------------------------------------- review follow-ups


async def test_delivered_is_shown_for_the_full_hold(hass: HomeAssistant, freezer) -> None:
    """Delivered tracking is never refetched, yet must not age out after 15 min."""
    freezer.move_to(NOW)
    entry, fetch = await _setup(hass, _data(), _tracking("HANDING_OVER", stops=0))
    fetch.return_value = _tracking("DELIVERED", stops=None)
    await _refresh(hass, entry, _data(orders=[]))
    for minutes in (10, 20, 29):
        await _move_to(hass, freezer, NOW + timedelta(minutes=minutes))
        await _refresh(hass, entry)
        assert _status(hass) == "delivered", minutes
    await _move_to(hass, freezer, NOW + timedelta(minutes=31))
    assert _status(hass) == STATE_UNKNOWN


async def test_latch_hard_timeout_is_final_for_a_stuck_delivery(
    hass: HomeAssistant, freezer
) -> None:
    """A delivery stuck in ARRIVED must not keep the latches on forever."""
    freezer.move_to(NOW)
    eta = NOW + timedelta(minutes=2)
    entry, fetch = await _setup(hass, _data(), _tracking("ARRIVED", eta=eta, stops=0))
    assert _state(hass, "binary_sensor", "courier_at_door").state == STATE_ON
    when = NOW
    while when < eta + timedelta(hours=1, minutes=5):
        when += timedelta(minutes=5)
        await _move_to(hass, freezer, when)
        await _refresh(hass, entry)
    assert _state(hass, "binary_sensor", "courier_at_door").state == STATE_OFF
    await _move_to(hass, freezer, when + timedelta(minutes=10))
    await _refresh(hass, entry)
    assert _state(hass, "binary_sensor", "courier_at_door").state == STATE_OFF
    assert _state(hass, "binary_sensor", "last_mile").state == STATE_OFF


async def test_last_mile_does_not_flap_while_queued(hass: HomeAssistant, freezer) -> None:
    """One stop left, tracked ETA moves later out of a 12-minute lead window."""
    from custom_components.rohlikcz.const import CONF_LAST_MILE_LEAD

    eta = SLOT_START - timedelta(minutes=50)  # early delivery allowed: not capped
    freezer.move_to(eta - timedelta(minutes=11))
    entry, fetch = await _setup(
        hass, _data(), _tracking("IN_QUEUE", eta=eta, stops=1), {CONF_LAST_MILE_LEAD: 12}
    )
    assert _state(hass, "binary_sensor", "last_mile").state == STATE_ON
    fetch.return_value = _tracking("IN_QUEUE", eta=eta + timedelta(minutes=3), stops=1)
    await _refresh(hass, entry)
    assert _state(hass, "binary_sensor", "last_mile").state == STATE_ON


async def test_restart_keeps_a_latched_last_mile_on(hass: HomeAssistant, freezer) -> None:
    """Latched while on the way, ETA then moved out of the lead: a reload keeps it on."""
    eta = NOW + timedelta(minutes=4)
    freezer.move_to(NOW)
    data = _data()
    entry, fetch = await _setup(hass, data, _tracking("ON_THE_WAY", eta=eta, stops=0))
    later = _tracking("ON_THE_WAY", eta=eta + timedelta(minutes=10), stops=0)
    fetch.return_value = later
    await _refresh(hass, entry)
    entity_id = _state(hass, "binary_sensor", "last_mile").entity_id
    assert hass.states.get(entity_id).state == STATE_ON

    seen: list[str] = []
    hass.bus.async_listen(
        "state_changed",
        lambda event: event.data["entity_id"] == entity_id
        and event.data["new_state"] is not None
        and seen.append(event.data["new_state"].state),
    )
    with patch(
        "custom_components.rohlikcz.hub.RohlikAPI.get_data",
        new=AsyncMock(return_value=data),
    ), patch(
        "rohlik_api.services.orders.OrderService.get_delivery_tracking",
        new=AsyncMock(return_value=later),
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_ON
    assert STATE_OFF not in seen


async def test_not_found_is_retried_after_30_minutes(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    entry, fetch = await _setup(hass, _data(), None)
    await _move_to(hass, freezer, NOW + timedelta(minutes=20))
    await _refresh(hass, entry)
    assert fetch.await_count == 1
    fetch.return_value = _tracking("IN_QUEUE")
    await _move_to(hass, freezer, NOW + timedelta(minutes=31))
    await _refresh(hass, entry)
    # The scheduled refresh at +30 min retries (2), then the manual one fetches (3).
    assert fetch.await_count == 3
    assert _status(hass) == "in_queue"


async def test_final_fetch_is_not_delayed_by_the_next_order(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    entry, fetch = await _setup(hass, _data(), _tracking("HANDING_OVER", stops=0))

    async def by_order(order_id, raise_on_error=False):
        return _tracking("DELIVERED", stops=None) if order_id == ORDER_ID else None

    fetch.side_effect = by_order
    await _refresh(hass, entry, _data(orders=[_order(7002, NOW + timedelta(hours=1))]))
    assert _status(hass) == "delivered"
    assert _state(hass, "binary_sensor", "courier_at_door").state == STATE_OFF


async def test_late_stage_without_estimate_is_bounded(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    entry, _fetch = await _setup(hass, _data(), _tracking("ON_THE_WAY", eta=None, stops=0))
    account = entry.runtime_data
    assert account._late_stage(dt_util.utcnow())
    slot_till = SLOT_START + timedelta(minutes=15)
    assert not account._late_stage(dt_util.as_utc(slot_till + timedelta(hours=1, minutes=1)))


def test_more_phone_formats_are_stripped() -> None:
    for number in (
        "700-000-001",
        "+420-700-000-001",
        "00420700000001",
        "700&nbsp;000&nbsp;001",
        "700 00 00 01",
        "0151 1234567",
        "0664 1234567",
        "06 30 123 4567",
    ):
        assert strip_html_and_phones(f"Volejte {number}. Díky!") == "Díky!", number
    assert strip_html_and_phones(
        'Zavolejte <a href="tel:+420000000000">kurýrovi</a>. Díky!'
    ) == "Díky!"
    for kept in ("Doručíme v 13:00.", "Cena 1 097,64 Kč.", "Doručeno 24.11.2026.", "Celkem 8 tašek."):
        assert strip_html_and_phones(kept) == kept


async def test_restored_delivery_info_is_sanitised(hass: HomeAssistant, freezer) -> None:
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import mock_restore_cache

    freezer.move_to(NOW)
    mock_restore_cache(
        hass,
        [
            State(
                "sensor.test_user_delivery_information",
                "Doručíme za 8 minut.",
                {"Additional Content": f"Veze 8 tašek. Volejte {PHONE}."},
            )
        ],
    )
    await _setup(hass, _data(announcements=[]))
    state = _state(hass, "sensor", "delivery_info")
    assert PHONE not in str(state.attributes)


async def test_old_eta_store_without_source_loads(hass: HomeAssistant, hass_storage) -> None:
    from custom_components.rohlikcz.eta import DeliveryEtaTracker

    hass_storage["rohlikcz_test_eta"] = {
        "version": 1,
        "key": "rohlikcz_test_eta",
        "data": {
            "last_value": NOW.isoformat(),
            "last_live_value": NOW.isoformat(),
            "last_live_order_id": str(ORDER_ID),
            "last_live_slot_since": SLOT_START.isoformat(),
            "announcement_arrivals": [],
        },
    }
    tracker = DeliveryEtaTracker(hass, "rohlikcz_test_eta", PRAGUE)
    await tracker.async_load()
    assert tracker.last_live_source == "announcement"
    assert tracker.last_live_order_id == str(ORDER_ID)


# ------------------------------------------------- second review follow-ups


async def test_queued_far_away_does_not_latch_last_mile(hass: HomeAssistant, freezer) -> None:
    """Many stops left: a close ETA that then slips by hours turns Last Mile off again."""
    from custom_components.rohlikcz.const import CONF_LAST_MILE_LEAD

    freezer.move_to(NOW)
    eta = NOW + timedelta(minutes=25)
    entry, fetch = await _setup(
        hass,
        _data([_order(since=NOW)]),
        _tracking("IN_QUEUE", eta=eta, stops=8, slot_since=NOW),
        {CONF_LAST_MILE_LEAD: 30},
    )
    assert _state(hass, "binary_sensor", "last_mile").state == STATE_ON
    fetch.return_value = _tracking("IN_QUEUE", eta=NOW + timedelta(minutes=150), stops=8, slot_since=NOW)
    await _refresh(hass, entry)
    assert _state(hass, "binary_sensor", "last_mile").state == STATE_OFF
    # Much later the courier really comes: it turns on again.
    await _move_to(hass, freezer, NOW + timedelta(minutes=140))
    fetch.return_value = _tracking("ON_THE_WAY", eta=NOW + timedelta(minutes=150), stops=0, slot_since=NOW)
    await _refresh(hass, entry)
    assert _state(hass, "binary_sensor", "last_mile").state == STATE_ON


async def test_timed_out_delivery_stays_off_after_reload(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    eta = NOW + timedelta(minutes=2)
    stuck = _tracking("ARRIVED", eta=eta, stops=0)
    data = _data()
    entry, _fetch = await _setup(hass, data, stuck)
    assert _state(hass, "binary_sensor", "courier_at_door").state == STATE_ON
    await _move_to(hass, freezer, eta + timedelta(hours=1, minutes=1))
    await _refresh(hass, entry)
    assert _state(hass, "binary_sensor", "courier_at_door").state == STATE_OFF
    with patch(
        "custom_components.rohlikcz.hub.RohlikAPI.get_data",
        new=AsyncMock(return_value=data),
    ), patch(
        "rohlik_api.services.orders.OrderService.get_delivery_tracking",
        new=AsyncMock(return_value=stuck),
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert _state(hass, "binary_sensor", "courier_at_door").state == STATE_OFF
    assert _state(hass, "binary_sensor", "last_mile").state == STATE_OFF


async def test_tracking_without_order_id_still_drives_everything(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(NOW)
    eta = NOW + timedelta(minutes=3)
    tracking = _tracking("ON_THE_WAY", eta=eta, stops=0)
    tracking.order_id = None  # the response lacked orderId; the hub knows the order
    await _setup(hass, _data(), tracking)
    remaining = _state(hass, "sensor", "delivery_time_remaining")
    assert remaining.attributes["source"] == "tracking"
    assert _state(hass, "binary_sensor", "last_mile").state == STATE_ON
    assert _state(hass, "sensor", "delivery_status").attributes["order_id"] == ORDER_ID


def test_dates_links_and_abbreviations_in_phone_stripping() -> None:
    for kept in (
        "Doručíme 24.11.2026 13:00 až 13:15.",
        "Lieferung 2026.11.24 13:00-13:15.",
        "Livrare 24/11/2026 13:00.",
        "Váš nákup doručíme přibližně za 206 minut, tedy v 13:00.",
    ):
        assert strip_html_and_phones(kept) == kept
    for dropped in (
        "Volejte 777 - 000 - 001. Díky!",
        'Napište <a href="https://wa.me/420000000000">sem</a>. Díky!',
        'Klik <a href=" tel:+420000000000">zde</a>. Díky!',
        "Volejte na tel. 700 000 001. Díky!",
    ):
        assert strip_html_and_phones(dropped) == "Díky!", dropped


# -------------------------------------------------- third review follow-ups


def test_dash_slash_dot_phone_numbers_are_not_mistaken_for_dates() -> None:
    for number in (
        "06-30-123-4567",
        "06/30/123-4567",
        "030/12/34 56 78",
        "0721-12-34-56",
        "777-12-34-56",
        "01.51.12.34.56",
        "777–000–001",
        "777‑000‑001",
    ):
        assert strip_html_and_phones(f"Volejte {number}. Díky!") == "Díky!", number
    for kept in ("Szállítás 2026.11.24. 13:00.", "Lieferung am 24.11. um 13:00."):
        assert strip_html_and_phones(kept) == kept


async def test_restart_does_not_latch_an_unlatched_last_mile(hass: HomeAssistant, freezer) -> None:
    """On only because of the window (queued, many stops): not restored as latched."""
    from custom_components.rohlikcz.const import CONF_LAST_MILE_LEAD

    freezer.move_to(NOW)
    data = _data([_order(since=NOW)])
    queued = _tracking("IN_QUEUE", eta=NOW + timedelta(minutes=20), stops=6, slot_since=NOW)
    entry, _fetch = await _setup(hass, data, queued, {CONF_LAST_MILE_LEAD: 30})
    state = _state(hass, "binary_sensor", "last_mile")
    assert state.state == STATE_ON and state.attributes["latched"] is False

    slipped = _tracking("IN_QUEUE", eta=NOW + timedelta(hours=3), stops=6, slot_since=NOW)
    with patch(
        "custom_components.rohlikcz.hub.RohlikAPI.get_data",
        new=AsyncMock(return_value=data),
    ), patch(
        "rohlik_api.services.orders.OrderService.get_delivery_tracking",
        new=AsyncMock(return_value=slipped),
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert _state(hass, "binary_sensor", "last_mile").state == STATE_OFF


async def test_next_order_is_fetched_when_the_delivered_display_ends(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(NOW)
    entry, fetch = await _setup(
        hass,
        _data(),
        _tracking("HANDING_OVER", stops=0),
        {CONF_AUTO_REFRESH_ETA: True, CONF_ETA_REFRESH_INTERVAL: 60},
    )
    fetch.return_value = _tracking("DELIVERED", stops=None)
    nxt = _data(orders=[_order(7002, NOW + timedelta(hours=3))])
    await _refresh(hass, entry, nxt)
    assert _status(hass) == "delivered"

    fetch.return_value = _tracking("ON_THE_WAY", order_id=7002, stops=0)
    await _move_to(hass, freezer, NOW + timedelta(minutes=5, seconds=2))
    assert _status(hass) == "on_the_way"
    assert _state(hass, "sensor", "delivery_status").attributes["order_id"] == 7002


async def test_latch_has_an_absolute_cap(hass: HomeAssistant, freezer) -> None:
    """ARRIVED with an estimate that keeps slipping still ends at slot end + 4 h."""
    freezer.move_to(NOW)
    entry, fetch = await _setup(hass, _data(), _tracking("ARRIVED", eta=NOW, stops=0))
    assert _state(hass, "binary_sensor", "courier_at_door").state == STATE_ON
    slot_till = SLOT_START + timedelta(minutes=15)
    when = NOW
    while when < slot_till + timedelta(hours=4, minutes=10):
        when += timedelta(minutes=10)
        fetch.return_value = _tracking("ARRIVED", eta=when + timedelta(minutes=30), stops=0)
        await _move_to(hass, freezer, when)
        await _refresh(hass, entry)
    assert _state(hass, "binary_sensor", "courier_at_door").state == STATE_OFF
    assert not entry.runtime_data._late_stage(dt_util.utcnow())


# ---------------------------------------------------- courier map and distance


def _enable_courier_tracker(hass: HomeAssistant) -> None:
    """Pre-register the (disabled-by-default) courier tracker as enabled."""
    er.async_get(hass).async_get_or_create(
        "device_tracker", DOMAIN, "123456_courier", suggested_object_id="courier"
    )


def _courier(hass: HomeAssistant):
    return _state(hass, "device_tracker", "courier")


def _distance(hass: HomeAssistant):
    return _state(hass, "sensor", "courier_distance")


async def test_courier_tracker_is_disabled_by_default(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    await _setup(hass, _data(), _tracking("ON_THE_WAY", stops=0))
    entity_id = er.async_get(hass).async_get_entity_id("device_tracker", DOMAIN, "123456_courier")
    entry = er.async_get(hass).async_get(entity_id)
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(entity_id) is None


async def test_courier_on_the_way_is_on_the_map(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    _enable_courier_tracker(hass)
    await _setup(hass, _data(), _tracking("ON_THE_WAY", stops=0))
    state = _courier(hass)
    assert state.attributes["latitude"] == 50.001
    assert state.attributes["longitude"] == 14.001
    assert state.attributes["source_type"] == "gps"
    assert state.attributes["order_id"] == ORDER_ID
    assert state.attributes["position_recorded"] == dt_util.utcnow() - timedelta(seconds=30)
    assert state.attributes.get("entity_category") is None
    registry_entry = er.async_get(hass).async_get(state.entity_id)
    assert registry_entry.entity_category is None
    # The courier's name ("Courier") is never exposed; the entity's own name is.
    attributes = {k: v for k, v in state.attributes.items() if k != "friendly_name"}
    assert COURIER not in str(attributes)
    assert PHONE not in str(state.attributes)
    # 0.001° lat ≈ 111 m and 0.001° lon at 50° N ≈ 71.5 m from the destination.
    distance = _distance(hass)
    assert distance.attributes["unit_of_measurement"] == "km"
    assert float(distance.state) == pytest.approx(0.132, abs=0.001)


@pytest.mark.parametrize("status", ["IN_QUEUE", "DELIVERED"])
async def test_no_courier_position_outside_the_last_mile(
    hass: HomeAssistant, freezer, status
) -> None:
    freezer.move_to(NOW)
    _enable_courier_tracker(hass)
    data = _data()
    await _setup(hass, data, _tracking(status, stops=None if status == "DELIVERED" else 3))
    state = _courier(hass)
    assert state.state == STATE_UNKNOWN
    assert "latitude" not in state.attributes
    assert _distance(hass).state == STATE_UNKNOWN


async def test_stale_position_is_not_shown(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    _enable_courier_tracker(hass)
    entry, fetch = await _setup(hass, _data(), _tracking("ON_THE_WAY", stops=0, stale=True))
    assert _courier(hass).state == STATE_UNKNOWN
    assert _distance(hass).state == STATE_UNKNOWN

    fetch.return_value = _tracking("ON_THE_WAY", stops=0)
    await entry.runtime_data.refresh_delivery_times()
    await hass.async_block_till_done()
    assert _courier(hass).attributes["latitude"] == 50.001
    assert _distance(hass).state != STATE_UNKNOWN


async def test_courier_move_updates_the_map(hass: HomeAssistant, freezer) -> None:
    """Only the position changes: the tracker and distance still update."""
    freezer.move_to(NOW)
    _enable_courier_tracker(hass)
    entry, fetch = await _setup(hass, _data(), _tracking("ON_THE_WAY", stops=0))
    account = entry.runtime_data
    account._client.delivery.get_announcements = AsyncMock(return_value=None)
    before = float(_distance(hass).state)

    fetch.return_value = _tracking("ON_THE_WAY", stops=0, position=(50.01, 14.0))
    await account.refresh_delivery_times()
    await hass.async_block_till_done()
    assert _courier(hass).attributes["latitude"] == 50.01
    after = float(_distance(hass).state)
    assert after == pytest.approx(1.112, abs=0.002)
    assert after > before


async def test_courier_at_home_zone(hass: HomeAssistant, freezer) -> None:
    """At the door the tracker enters the home zone, for zone automations."""
    hass.config.latitude, hass.config.longitude = 50.0, 14.0
    await hass.config.async_update(latitude=50.0, longitude=14.0, radius=100)
    await hass.async_block_till_done()
    freezer.move_to(NOW)
    _enable_courier_tracker(hass)
    await _setup(hass, _data(), _tracking("ARRIVED", stops=0, position=(50.0001, 14.0001)))
    assert _courier(hass).state == "home"


async def test_distance_unknown_without_destination(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    tracking = _tracking("ON_THE_WAY", stops=0)
    tracking.destination = None
    _enable_courier_tracker(hass)
    await _setup(hass, _data(), tracking)
    assert _distance(hass).state == STATE_UNKNOWN
    assert _courier(hass).attributes["latitude"] == 50.001


async def test_diagnostics_hide_the_courier_tracker_position(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    _enable_courier_tracker(hass)
    entry, _fetch = await _setup(hass, _data(), _tracking("ON_THE_WAY", stops=0))
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    assert "50.001" not in text and "14.001" not in text
    assert "50.0," not in text and "14.0," not in text
    tracking = diag["coordinator"]["delivery_tracking"]
    for key in ("latitude", "longitude", "recorded_at", "destination", "courier_position"):
        assert key not in str(tracking), key


async def test_old_position_is_hidden_and_expires_on_time(hass: HomeAssistant, freezer) -> None:
    """A position is shown for at most 5 minutes, even if nothing is fetched."""
    freezer.move_to(NOW)
    _enable_courier_tracker(hass)
    await _setup(hass, _data(), _tracking("ON_THE_WAY", stops=0, recorded_ago=timedelta(minutes=1)))
    assert _courier(hass).attributes["latitude"] == 50.001
    # 4 min later the position is 5 min old: the timer flips both to unknown.
    await _move_to(hass, freezer, NOW + timedelta(minutes=4, seconds=2))
    assert _courier(hass).state == STATE_UNKNOWN
    assert _distance(hass).state == STATE_UNKNOWN
    # The tracking itself is still valid: the status is still shown.
    assert _status(hass) == "on_the_way"


async def test_position_older_than_max_age_is_never_shown(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    _enable_courier_tracker(hass)
    await _setup(hass, _data(), _tracking("ON_THE_WAY", stops=0, recorded_ago=timedelta(minutes=6)))
    assert _courier(hass).state == STATE_UNKNOWN
    assert _distance(hass).state == STATE_UNKNOWN


async def test_position_without_timestamp_ages_from_the_fetch(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    _enable_courier_tracker(hass)
    entry, fetch = await _setup(
        hass, _data(), _tracking("ON_THE_WAY", stops=0, recorded_ago=None)
    )
    assert _courier(hass).attributes["latitude"] == 50.001
    assert _courier(hass).attributes["position_recorded"] is None
    # Fetches fail from now on: the frozen position goes after 5 minutes.
    fetch.side_effect = APIRequestFailedError("boom")
    await entry.runtime_data.refresh_delivery_times()
    await _move_to(hass, freezer, NOW + timedelta(minutes=5, seconds=2))
    assert _courier(hass).state == STATE_UNKNOWN
    assert _courier(hass).state != STATE_UNAVAILABLE


async def test_future_timestamp_counts_from_the_fetch(hass: HomeAssistant, freezer) -> None:
    """A shop clock ahead of ours does not keep a position longer."""
    freezer.move_to(NOW)
    _enable_courier_tracker(hass)
    await _setup(
        hass, _data(), _tracking("ON_THE_WAY", stops=0, recorded_ago=-timedelta(minutes=30))
    )
    assert _courier(hass).attributes["latitude"] == 50.001
    await _move_to(hass, freezer, NOW + timedelta(minutes=5, seconds=2))
    assert _courier(hass).state == STATE_UNKNOWN


async def test_missing_stale_flag_counts_as_current(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    _enable_courier_tracker(hass)
    await _setup(hass, _data(), _tracking("ON_THE_WAY", stops=0, stale=None))
    assert _courier(hass).attributes["latitude"] == 50.001


async def test_position_disappears_when_delivered(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    _enable_courier_tracker(hass)
    entry, fetch = await _setup(hass, _data(), _tracking("HANDING_OVER", stops=0))
    entry.runtime_data._client.delivery.get_announcements = AsyncMock(return_value=None)
    assert _courier(hass).attributes["latitude"] == 50.001
    fetch.return_value = _tracking("DELIVERED", stops=None)
    entry.runtime_data.async_request_refresh = AsyncMock()
    await entry.runtime_data.refresh_delivery_times()
    await hass.async_block_till_done()
    assert _status(hass) == "delivered"
    assert _courier(hass).state == STATE_UNKNOWN
    assert "latitude" not in _courier(hass).attributes
    assert _distance(hass).state == STATE_UNKNOWN


async def test_failed_refresh_keeps_the_tracker_available(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    _enable_courier_tracker(hass)
    entry, _fetch = await _setup(hass, _data(), _tracking("ON_THE_WAY", stops=0))
    entry.runtime_data._client.get_data = AsyncMock(side_effect=APIRequestFailedError("down"))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert entry.runtime_data.last_update_success is False
    assert _courier(hass).attributes["latitude"] == 50.001
    assert _distance(hass).state != STATE_UNAVAILABLE


async def test_stationary_courier_updates_position_recorded(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    _enable_courier_tracker(hass)
    entry, fetch = await _setup(hass, _data(), _tracking("ON_THE_WAY", stops=0))
    account = entry.runtime_data
    account._client.delivery.get_announcements = AsyncMock(return_value=None)
    first = _courier(hass).attributes["position_recorded"]
    freezer.move_to(NOW + timedelta(minutes=1))
    fetch.return_value = _tracking("ON_THE_WAY", stops=0)  # same place, newer timestamp
    await account.refresh_delivery_times()
    await hass.async_block_till_done()
    assert _courier(hass).attributes["position_recorded"] == first + timedelta(minutes=1)


async def test_unchanged_unknown_tracker_is_not_rewritten(hass: HomeAssistant, freezer) -> None:
    """No recorder row per refresh while no delivery is under way."""
    freezer.move_to(NOW)
    _enable_courier_tracker(hass)
    entry, _fetch = await _setup(hass, _data(), _tracking("IN_QUEUE"))
    before = _courier(hass)
    assert before.state == STATE_UNKNOWN
    freezer.move_to(NOW + timedelta(minutes=1))
    await _refresh(hass, entry)
    after = _courier(hass)
    assert after.last_updated == before.last_updated


def _courier_entities(hass: HomeAssistant) -> list:
    """The live courier tracker and distance sensor entity objects."""
    ids = {_courier(hass).entity_id, _distance(hass).entity_id}
    return [
        entity
        for platform in entity_platform.async_get_platforms(hass, DOMAIN)
        for entity_id, entity in platform.entities.items()
        if entity_id in ids
    ]


def _assert_no_past_timer(hass: HomeAssistant) -> None:
    """Each courier entity's timer is unarmed or set in the future (no busy loop)."""
    entities = _courier_entities(hass)
    assert len(entities) == 2
    now = dt_util.utcnow()
    for entity in entities:
        when = entity._next_change()
        assert when is None or when > now, entity.entity_id


async def test_expired_position_does_not_rearm_a_past_timer(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    _enable_courier_tracker(hass)
    entry, _fetch = await _setup(
        hass, _data(), _tracking("ON_THE_WAY", stops=0, recorded_ago=timedelta(minutes=1))
    )
    _assert_no_past_timer(hass)
    await _move_to(hass, freezer, NOW + timedelta(minutes=4, seconds=2))
    assert _courier(hass).state == STATE_UNKNOWN
    _assert_no_past_timer(hass)
    # A coordinator update after expiry re-arms only for the tracking expiry.
    entry.runtime_data.async_update_listeners()
    await hass.async_block_till_done()
    _assert_no_past_timer(hass)


async def test_position_already_old_at_setup_does_not_spin(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    _enable_courier_tracker(hass)
    await _setup(hass, _data(), _tracking("ON_THE_WAY", stops=0, recorded_ago=timedelta(minutes=6)))
    _assert_no_past_timer(hass)
    writes = []
    for entity in _courier_entities(hass):
        original = entity.async_write_ha_state
        entity.async_write_ha_state = lambda original=original: (writes.append(1), original())
    for seconds in (1, 2, 3):
        await _move_to(hass, freezer, NOW + timedelta(seconds=seconds))
    assert writes == []


async def test_eta_timed_entity_never_arms_a_past_moment(hass: HomeAssistant, freezer) -> None:
    """The base-class guard: a past _next_change() arms no timer and writes nothing."""
    from custom_components.rohlikcz.entity import EtaTimedEntity

    freezer.move_to(NOW)
    entry, _fetch = await _setup(hass, _data(), _tracking("IN_QUEUE"))

    class PastEntity(EtaTimedEntity):
        _attr_translation_key = "past_probe"

        def _next_change(self):
            return dt_util.utcnow() - timedelta(seconds=1)

    entity = PastEntity(entry.runtime_data)
    entity.hass = hass
    writes = []
    entity.async_write_ha_state = lambda: writes.append(1)
    entity._schedule_update()
    assert entity._unsub_timer is None
    for seconds in (1, 2, 3):
        await _move_to(hass, freezer, NOW + timedelta(seconds=seconds))
    assert writes == []
