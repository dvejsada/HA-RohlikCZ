"""Tests for setup, coordinator behaviour, and unloading."""
from __future__ import annotations

import json
import os
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from rohlik_api import APIRequestFailedError, InvalidCredentialsError

from custom_components.rohlikcz.const import CONF_ANALYTICS, DOMAIN
from custom_components.rohlikcz.hub import OrderStore, RohlikAccount

from fixtures_data import sample_api_data

ENTRY_DATA = {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"}


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, unique_id="123456", data=ENTRY_DATA, options={}
    )


def _patch_get_data(side_effect=None, return_value=None):
    return patch(
        "custom_components.rohlikcz.hub.RohlikAPI.get_data",
        new=AsyncMock(side_effect=side_effect, return_value=return_value),
    )


async def test_setup_creates_entities_and_unloads(hass: HomeAssistant) -> None:
    """A successful setup loads the entry, populates runtime_data and entities."""
    entry = _entry()
    entry.add_to_hass(hass)

    with _patch_get_data(return_value=sample_api_data()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, RohlikAccount)
    assert entry.runtime_data.last_update_success is True

    ent_reg = er.async_get(hass)
    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    # Sensors + binary sensors + todo + calendar should all be registered.
    assert len(entities) > 10

    # Coordinator data should flow through to entity state.
    reusable_id = ent_reg.async_get_entity_id(
        "binary_sensor", DOMAIN, "123456_is_reusable"
    )
    assert reusable_id is not None
    assert hass.states.get(reusable_id).state == "on"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_updated_sensor_reflects_last_refresh(hass: HomeAssistant) -> None:
    """The 'updated' sensor shows the coordinator's last fetch time, not now()."""
    entry = _entry()
    entry.add_to_hass(hass)

    with _patch_get_data(return_value=sample_api_data()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    account = entry.runtime_data
    ent_reg = er.async_get(hass)
    updated_id = ent_reg.async_get_entity_id("sensor", DOMAIN, "123456_updated")
    assert updated_id is not None

    state = hass.states.get(updated_id)
    assert account.last_refresh is not None
    # Timestamp sensors report second precision; compare the same instant.
    assert dt_util.parse_datetime(state.state) == account.last_refresh.replace(
        microsecond=0
    )


async def test_calendar_events_available_immediately(hass: HomeAssistant) -> None:
    """Calendar events are populated on setup, before any later refresh."""
    data = sample_api_data()
    start = (dt_util.now() + timedelta(days=1)).replace(microsecond=0)
    end = start + timedelta(hours=2)
    data["next_order"] = [
        {
            "id": 7001,
            "deliverySlot": {"since": start.isoformat(), "till": end.isoformat()},
        }
    ]

    entry = _entry()
    entry.add_to_hass(hass)
    with _patch_get_data(return_value=data):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    cal_id = ent_reg.async_get_entity_id("calendar", DOMAIN, "123456_delivery_calendar")
    assert cal_id is not None

    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": cal_id,
            "start_date_time": (start - timedelta(hours=1)).isoformat(),
            "end_date_time": (end + timedelta(hours=1)).isoformat(),
        },
        blocking=True,
        return_response=True,
    )
    assert len(response[cal_id]["events"]) == 1


async def test_setup_auth_failure_triggers_reauth(hass: HomeAssistant) -> None:
    """Invalid credentials during setup start a reauth flow."""
    entry = _entry()
    entry.add_to_hass(hass)

    with _patch_get_data(side_effect=InvalidCredentialsError("bad creds")):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == "reauth"
    ]
    assert len(flows) == 1


async def test_setup_connection_error_retries(hass: HomeAssistant) -> None:
    """A transient API failure puts the entry into retry state."""
    entry = _entry()
    entry.add_to_hass(hass)

    with _patch_get_data(side_effect=APIRequestFailedError("no network")):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_monthly_spent_sums_current_month(hass: HomeAssistant) -> None:
    """MonthlySpent totals current-month delivered orders (pure native_value)."""
    now = dt_util.now()
    data = sample_api_data()
    data["delivered_orders"] = [
        {
            "id": 8001,
            "orderTime": now.strftime("%Y-%m-15T10:00:00.000%z"),
            "priceComposition": {"total": {"amount": 500.0}},
        }
    ]

    entry = _entry()
    entry.add_to_hass(hass)
    with _patch_get_data(return_value=data):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    spent_id = ent_reg.async_get_entity_id("sensor", DOMAIN, "123456_monthly_spent")
    assert spent_id is not None
    assert hass.states.get(spent_id).state == "500.0"


async def test_refresh_slots_updates_express_sensor(hass: HomeAssistant) -> None:
    """refresh_slots merges fresh slot data and the express sensor reflects it."""
    data = sample_api_data()
    data["next_delivery_slot"] = None  # express unavailable initially

    entry = _entry()
    entry.add_to_hass(hass)
    with _patch_get_data(return_value=data):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    express_id = ent_reg.async_get_entity_id(
        "binary_sensor", DOMAIN, "123456_is_express_available"
    )
    assert hass.states.get(express_id).state == "off"

    account = entry.runtime_data
    fresh_slots = {"data": {"expressSlot": {"timeSlotCapacityDTO": {"totalFreeCapacityPercent": 80}}}}
    account._client.delivery.get_next_slots = AsyncMock(return_value=fresh_slots)

    await account.refresh_slots()
    await hass.async_block_till_done()

    # Only the slot data changed; the express sensor flips to available.
    assert account.data["next_delivery_slot"] == fresh_slots
    assert hass.states.get(express_id).state == "on"
    # Other data is untouched.
    assert account.data["login"]["data"]["user"]["id"] == 123456


async def test_slot_sensors_registered(hass: HomeAssistant) -> None:
    """The three preselected-slot sensors register and parse their data."""
    data = sample_api_data()
    data["next_delivery_slot"] = {
        "data": {
            "preselectedSlots": [
                {
                    "type": "EXPRESS",
                    "price": 49,
                    "title": "Express",
                    "subtitle": "soon",
                    "slot": {
                        "interval": {"since": "2026-06-28T08:00:00+02:00", "till": "2026-06-28T10:00:00+02:00"},
                        "timeSlotCapacityDTO": {"totalFreeCapacityPercent": 50, "capacityMessage": "ok"},
                    },
                },
                {
                    "type": "FIRST",
                    "slot": {"interval": {"since": "2026-06-28T12:00:00+02:00", "till": "2026-06-28T14:00:00+02:00"}, "timeSlotCapacityDTO": {}},
                },
                {
                    "type": "ECO",
                    "slot": {"interval": {"since": "2026-06-28T16:00:00+02:00", "till": "2026-06-28T18:00:00+02:00"}, "timeSlotCapacityDTO": {}},
                },
            ]
        }
    }

    entry = _entry()
    entry.add_to_hass(hass)
    with _patch_get_data(return_value=data):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    for key in ("express_slot", "standard_slot", "eco_slot"):
        assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"123456_{key}") is not None, key

    # The express slot's data flows to state + attributes.
    eid = ent_reg.async_get_entity_id("sensor", DOMAIN, "123456_express_slot")
    state = hass.states.get(eid)
    assert state.state not in (None, "unknown", "unavailable")
    assert state.attributes.get("Remaining Capacity Percent") == 50


async def test_spending_breakdown_sensors_registered(hass: HomeAssistant, hass_storage) -> None:
    """All category/item spending sensors register with their stable unique_ids."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        data=ENTRY_DATA,
        options={
            CONF_ANALYTICS: [
                "categories_l0",
                "categories_l1",
                "categories_l2",
                "categories_l3",
                "per_item",
            ]
        },
    )
    entry.add_to_hass(hass)

    no_network = {
        "fetch_full_order_history": AsyncMock(return_value={}),
        "_fetch_order_items": AsyncMock(return_value={}),
        "_fetch_product_categories": AsyncMock(return_value={}),
    }
    with _patch_get_data(return_value=sample_api_data()), patch.multiple(
        "custom_components.rohlikcz.hub.RohlikAccount", **no_network
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    expected = [
        "categories_l0_this_year", "categories_l0_all_time",
        "categories_this_year", "categories_all_time",
        "categories_l2_this_year", "categories_l2_all_time",
        "categories_l3_this_year", "categories_l3_all_time",
        "items_this_year", "items_all_time",
    ]
    for key in expected:
        assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"123456_{key}") is not None, key

    # The generic sensor reports 0 distinct categories on an unenriched store,
    # with the empty-case attributes present.
    eid = ent_reg.async_get_entity_id("sensor", DOMAIN, "123456_categories_all_time")
    state = hass.states.get(eid)
    assert state.state == "0"
    assert "enriched_orders" in state.attributes

    # items_all_time returns no attributes at all on an empty store.
    eid_items = ent_reg.async_get_entity_id("sensor", DOMAIN, "123456_items_all_time")
    state_items = hass.states.get(eid_items)
    assert state_items.state == "0"
    assert state_items.attributes.get("items") is None


async def test_order_store_imports_legacy_file(hass: HomeAssistant, hass_storage) -> None:
    """A pre-Store JSON file is imported once into Store and then removed."""
    storage_dir = hass.config.path(".storage")
    os.makedirs(storage_dir, exist_ok=True)
    legacy_path = os.path.join(storage_dir, "rohlikcz_999_orders.json")
    legacy = {
        "version": 1,
        "user_id": "999",
        "tracking_since": "2026-01-01T00:00:00+01:00",
        "orders": {"1": {"date": "2026-01-15", "amount": 100.0}},
    }
    with open(legacy_path, "w") as f:
        json.dump(legacy, f)

    store = await OrderStore.async_create(hass, "999")

    # Orders imported.
    assert store.alltime_count() == 1
    assert store.alltime_total() == 100.0
    # v1 -> v3 schema fields filled in.
    assert store.backfill_complete is True  # tracking_since was set
    assert "product_categories" in store._data
    # Legacy file removed; data now lives in HA Store.
    assert not os.path.exists(legacy_path)
    assert "rohlikcz_999_orders" in hass_storage


async def test_auto_enrich_applies_items_and_categories(hass: HomeAssistant, hass_storage) -> None:
    """Auto-enrichment fetches items + categories and persists them.

    Also guards the refactor that releases the store lock during network I/O.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        data=ENTRY_DATA,
        options={CONF_ANALYTICS: ["categories_l1"]},
    )
    entry.add_to_hass(hass)

    account = RohlikAccount(
        hass, "user", "pass", analytics=["categories_l1"], entry=entry
    )
    store = await OrderStore.async_create(hass, "123456")
    store.process_orders(
        [
            {
                "id": 9001,
                "orderTime": "2026-05-01T10:00:00.000+02:00",
                "priceComposition": {"total": {"amount": 750.0}},
            }
        ]
    )
    account._order_store = store
    account._fetch_order_items = AsyncMock(
        return_value={
            "9001": [
                {
                    "id": 111,
                    "name": "Milk",
                    "quantity": 1,
                    "price": 20.0,
                    "unit_price": 20.0,
                    "textual_amount": "1 l",
                }
            ]
        }
    )
    account._fetch_product_categories = AsyncMock(
        return_value={111: [{"level": 1, "name": "Dairy"}]}
    )

    await account._auto_enrich_new_orders(1)

    assert "items" in store.orders["9001"]
    assert store.get_product_category(111, 1) == "Dairy"


async def test_enrich_order_details_applies_results(hass: HomeAssistant, hass_storage) -> None:
    """The enrich_orders service path enriches items + categories.

    Guards the lock-split refactor in _enrich_order_details / enrich_order_details.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        data=ENTRY_DATA,
        options={CONF_ANALYTICS: ["categories_l1"]},
    )
    entry.add_to_hass(hass)

    account = RohlikAccount(
        hass, "user", "pass", analytics=["categories_l1"], entry=entry
    )
    store = await OrderStore.async_create(hass, "123456")
    store.process_orders(
        [
            {
                "id": 9002,
                "orderTime": "2026-05-02T10:00:00.000+02:00",
                "priceComposition": {"total": {"amount": 300.0}},
            }
        ]
    )
    account._order_store = store
    account._fetch_order_items = AsyncMock(
        return_value={
            "9002": [
                {
                    "id": 222,
                    "name": "Bread",
                    "quantity": 2,
                    "price": 40.0,
                    "unit_price": 20.0,
                    "textual_amount": "500 g",
                }
            ]
        }
    )
    account._fetch_product_categories = AsyncMock(
        return_value={222: [{"level": 1, "name": "Bakery"}]}
    )

    # hass=None keeps it off the persistent_notification service.
    result = await account.enrich_order_details()

    assert "items" in store.orders["9002"]
    assert store.get_product_category(222, 1) == "Bakery"
    assert result["orders_enriched_this_run"] == 1
    assert result["products_categorized_this_run"] == 1


async def test_coordinator_refresh_updates_data(hass: HomeAssistant) -> None:
    """Calling async_update refreshes coordinator data from the API."""
    entry = _entry()
    entry.add_to_hass(hass)

    first = sample_api_data()
    second = sample_api_data()
    second["cart"].total_items = 3

    mocked = AsyncMock(side_effect=[first, second])
    with patch(
        "custom_components.rohlikcz.hub.RohlikAPI.get_data", new=mocked
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        account = entry.runtime_data
        assert account.data["cart"].total_items == 0

        await account.async_update()
        await hass.async_block_till_done()
        assert account.data["cart"].total_items == 3
