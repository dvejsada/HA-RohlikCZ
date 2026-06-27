"""Tests for the diagnostics platform."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rohlikcz.const import DOMAIN
from custom_components.rohlikcz.diagnostics import (
    async_get_config_entry_diagnostics,
)

from fixtures_data import sample_api_data

ENTRY_DATA = {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"}


async def test_diagnostics_redacts_personal_data(hass: HomeAssistant) -> None:
    """Diagnostics include coordinator data but redact credentials/personal info."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="123456", data=ENTRY_DATA, options={}
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.rohlikcz.hub.RohlikAPI.get_data",
        new=AsyncMock(return_value=sample_api_data()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)

    # Credentials redacted in entry data.
    assert diag["entry"]["data"][CONF_PASSWORD] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_EMAIL] == "**REDACTED**"

    # Personal fields redacted within nested coordinator data.
    user = diag["data"]["login"]["data"]["user"]
    assert user["name"] == "**REDACTED**"
    assert user["email"] == "**REDACTED**"
    assert user["phone"] == "**REDACTED**"

    # Non-sensitive coordinator metadata is present.
    assert diag["coordinator"]["last_update_success"] is True
    assert diag["coordinator"]["analytics"] == []
