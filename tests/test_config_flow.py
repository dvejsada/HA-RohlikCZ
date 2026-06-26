"""Tests for the Rohlik.cz config and reauth flows."""
from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rohlikcz.const import DOMAIN
from custom_components.rohlikcz.errors import InvalidCredentialsError

VALID = {"title": "Test User", "user_id": "123456"}
USER_INPUT = {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"}


async def test_user_flow_success(hass: HomeAssistant) -> None:
    """Full happy-path: credentials -> analytics -> entry created."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(
        "custom_components.rohlikcz.config_flow.validate_input", return_value=VALID
    ), patch(
        "custom_components.rohlikcz.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        assert result["step_id"] == "analytics"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Test User"
    assert result["result"].unique_id == "123456"
    assert result["data"] == USER_INPUT


async def test_user_flow_invalid_auth(hass: HomeAssistant) -> None:
    """Wrong credentials surface an invalid_auth error on the form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.rohlikcz.config_flow.validate_input",
        side_effect=InvalidCredentialsError("bad"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_unknown_error(hass: HomeAssistant) -> None:
    """Unexpected errors surface as 'unknown'."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.rohlikcz.config_flow.validate_input",
        side_effect=RuntimeError("boom"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_duplicate_account_aborts(hass: HomeAssistant) -> None:
    """A second entry for the same account id aborts."""
    MockConfigEntry(domain=DOMAIN, unique_id="123456", data=USER_INPUT).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.rohlikcz.config_flow.validate_input", return_value=VALID
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_success(hass: HomeAssistant) -> None:
    """Reauth updates the password and reloads the entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "old"},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch(
        "custom_components.rohlikcz.config_flow.validate_input", return_value=VALID
    ), patch("custom_components.rohlikcz.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"


async def test_reauth_wrong_account(hass: HomeAssistant) -> None:
    """Reauth with a different account id aborts with wrong_account."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "old"},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with patch(
        "custom_components.rohlikcz.config_flow.validate_input",
        return_value={"title": "Other", "user_id": "999999"},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
