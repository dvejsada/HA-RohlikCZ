"""Tests for the favourites and delivery-slot sensors and services.

All payloads are made up. No real account data.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from rohlik_api import ProductCard

from custom_components.rohlikcz.const import DOMAIN
from custom_components.rohlikcz.hub import FAVOURITES_INTERVAL

from fixtures_data import sample_api_data

ENTRY_DATA = {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"}
IMG = "https://cdn.example.test/images/grocery/products/3001/3001-1.jpg"

FAVOURITES = [
    ProductCard(id=3001, name="Testjoghurt", brand=None, amount="250 g", unit="g",
                price=1.29, original_price=1.29, unit_price=5.16, currency="EUR",
                image=IMG),
    ProductCard(id=3002, name="Testsaft", brand=None, amount="1 l", unit="l",
                price=2.0, original_price=2.5, unit_price=2.0, currency="EUR",
                on_sale=True, in_stock=False),
]


def _slot(sid, typ, since, till, free, premium=False):
    return {
        "slotId": sid, "type": typ, "premium": premium, "eco": False, "price": 0,
        "timeSlotCapacityDTO": {"totalFreeCapacityPercent": free, "capacityMessage": "x"},
        "timeSlotReservationDTO": {"enabled": True, "duration": 60},
        "interval": {"since": since, "till": till},
    }


def _slots_payload() -> dict:
    day = (dt_util.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "status": 200,
        "data": {
            "preselectedSlots": [],
            "availabilityDays": [{
                "date": day, "label": "Morgen",
                "slots": {"10": [
                    _slot(1, "VIRTUAL", f"{day}T10:00:00+01:00", f"{day}T11:00:00+01:00", 40),
                    _slot(2, "ON_TIME", f"{day}T10:00:01+01:00", f"{day}T10:15:00+01:00", 0, True),
                    _slot(3, "ON_TIME", f"{day}T10:15:01+01:00", f"{day}T10:30:00+01:00", 20, True),
                ]},
            }],
        },
    }


async def _setup(hass: HomeAssistant, data: dict, favourites: AsyncMock) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="123456", data=ENTRY_DATA, options={})
    entry.add_to_hass(hass)
    with (
        patch("custom_components.rohlikcz.hub.RohlikAPI.get_data",
              new=AsyncMock(return_value=data)),
        patch("rohlik_api.services.products.ProductService.get_favourites", new=favourites),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _state(hass: HomeAssistant, key: str):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"123456_{key}")
    assert entity_id, key
    return hass.states.get(entity_id)


async def test_favourites_sensor(hass: HomeAssistant) -> None:
    favourites = AsyncMock(return_value=FAVOURITES)
    await _setup(hass, sample_api_data(), favourites)

    state = _state(hass, "favourites")
    assert state.state == "2"
    products = state.attributes["products"]
    assert products[0] == {
        "id": 3001, "name": "Testjoghurt", "amount": "250 g", "price": 1.29,
        "currency": "EUR", "in_stock": True, "on_sale": False, "image": IMG,
    }
    assert products[1]["in_stock"] is False and products[1]["on_sale"] is True
    assert state.attributes["updated"]


async def test_favourites_unavailable_when_fetch_fails(hass: HomeAssistant) -> None:
    await _setup(hass, sample_api_data(), AsyncMock(return_value=None))
    assert _state(hass, "favourites").state == "unavailable"


async def test_favourites_fetch_error_does_not_break_setup(hass: HomeAssistant) -> None:
    entry = await _setup(hass, sample_api_data(), AsyncMock(side_effect=RuntimeError("boom")))
    assert entry.state.value == "loaded"
    assert _state(hass, "favourites").state == "unavailable"


async def test_favourites_only_refetched_hourly(hass: HomeAssistant) -> None:
    favourites = AsyncMock(return_value=FAVOURITES)
    entry = await _setup(hass, sample_api_data(), favourites)
    account = entry.runtime_data
    with patch("rohlik_api.services.products.ProductService.get_favourites", new=favourites):
        assert await account._update_favourites() is False
        assert favourites.await_count == 1
        account._favourites_fetched -= FAVOURITES_INTERVAL
        assert await account._update_favourites() is True
        assert favourites.await_count == 2


async def test_get_favourites_service(hass: HomeAssistant) -> None:
    favourites = AsyncMock(return_value=FAVOURITES)
    entry = await _setup(hass, sample_api_data(), favourites)
    with patch("rohlik_api.services.products.ProductService.get_favourites", new=favourites):
        result = await hass.services.async_call(
            DOMAIN, "get_favourites", {"config_entry_id": entry.entry_id},
            blocking=True, return_response=True,
        )
    assert [p["id"] for p in result["products"]] == [3001, 3002]
    assert favourites.await_count == 2  # forced, not waiting an hour


async def test_delivery_slots_sensor_and_service(hass: HomeAssistant) -> None:
    data = sample_api_data()
    data["next_delivery_slot"] = _slots_payload()
    entry = await _setup(hass, data, AsyncMock(return_value=[]))

    state = _state(hass, "delivery_slots")
    assert state.state == "2"  # two windows with free capacity
    slots = state.attributes["slots"]
    assert [s["id"] for s in slots] == [1, 2, 3]
    assert slots[0]["minutes"] == 60 and slots[0]["free"] == 40 and slots[0]["day"] == "Morgen"
    assert slots[1]["minutes"] == 15 and slots[1]["premium"] is True and slots[1]["free"] == 0

    result = await hass.services.async_call(
        DOMAIN, "get_delivery_slots",
        {"config_entry_id": entry.entry_id, "only_available": True},
        blocking=True, return_response=True,
    )
    assert [s["id"] for s in result["slots"]] == [1, 3]


async def test_big_attributes_not_recorded(hass: HomeAssistant) -> None:
    from custom_components.rohlikcz.sensor import DeliverySlotsSensor, FavouritesSensor

    assert "slots" in DeliverySlotsSensor._unrecorded_attributes
    assert "products" in FavouritesSensor._unrecorded_attributes
