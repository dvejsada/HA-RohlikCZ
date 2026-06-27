"""Tests for setup, coordinator behaviour, and unloading."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rohlikcz.const import CONF_ANALYTICS, DOMAIN
from custom_components.rohlikcz.errors import APIRequestFailedError, InvalidCredentialsError
from custom_components.rohlikcz.hub import OrderStore, RohlikAccount

from fixtures_data import sample_api_data

ENTRY_DATA = {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"}


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, unique_id="123456", data=ENTRY_DATA, options={}
    )


def _patch_get_data(side_effect=None, return_value=None):
    return patch(
        "custom_components.rohlikcz.rohlik_api.RohlikCZAPI.get_data",
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


async def test_auto_enrich_applies_items_and_categories(hass: HomeAssistant, tmp_path) -> None:
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
    store = await OrderStore.async_create(str(tmp_path), "123456", hass)
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
    account._rohlik_api.enrich_orders_with_items = AsyncMock(
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
    account._rohlik_api.fetch_product_categories_batch = AsyncMock(
        return_value={111: [{"level": 1, "name": "Dairy"}]}
    )

    await account._auto_enrich_new_orders(1)

    assert "items" in store.orders["9001"]
    assert store.get_product_category(111, 1) == "Dairy"


async def test_enrich_order_details_applies_results(hass: HomeAssistant, tmp_path) -> None:
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
    store = await OrderStore.async_create(str(tmp_path), "123456", hass)
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
    account._rohlik_api.enrich_orders_with_items = AsyncMock(
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
    account._rohlik_api.fetch_product_categories_batch = AsyncMock(
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
    second["cart"]["total_items"] = 3

    mocked = AsyncMock(side_effect=[first, second])
    with patch(
        "custom_components.rohlikcz.rohlik_api.RohlikCZAPI.get_data", new=mocked
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        account = entry.runtime_data
        assert account.data["cart"]["total_items"] == 0

        await account.async_update()
        await hass.async_block_till_done()
        assert account.data["cart"]["total_items"] == 3
