"""Tests for live delivery tracking: fetching, ETA source, entities and privacy."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from homeassistant.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
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
            "courierLocationStale": False,
            "courierPosition": (
                {"latitude": 50.001, "longitude": 14.001, "recordedAt": None}
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


async def test_restart_mid_delivery_keeps_courier_at_door_on(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    data = _data()
    entry, _fetch = await _setup(hass, data, _tracking("ARRIVED", stops=0))
    entity_id = _state(hass, "binary_sensor", "courier_at_door").entity_id
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
        new=AsyncMock(return_value=_tracking("ARRIVED", stops=0)),
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == STATE_ON
    assert STATE_OFF not in seen


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
    await _refresh(hass, entry, _data(orders=[_order(7002, NOW + timedelta(hours=1))]))
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
    assert strip_html_and_phones("Objednávka 1000000001 je na cestě.") == (
        "Objednávka 1000000001 je na cestě."
    )


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
