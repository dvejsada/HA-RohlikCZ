"""Tests for setup, coordinator behaviour, and unloading."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rohlikcz.const import DOMAIN
from custom_components.rohlikcz.errors import APIRequestFailedError, InvalidCredentialsError
from custom_components.rohlikcz.hub import RohlikAccount

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
