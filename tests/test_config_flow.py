"""Tests for the Rohlik.cz config and reauth flows."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from rohlik_api import InvalidCredentialsError, RohlikAPIError

from custom_components.rohlikcz.config_flow import validate_input
from custom_components.rohlikcz.const import CONF_SITE, DOMAIN

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
    # The shop defaults to Rohlík.cz when not picked.
    assert result["data"] == {**USER_INPUT, CONF_SITE: "cz"}


async def test_user_flow_other_site(hass: HomeAssistant) -> None:
    """Picking another shop validates against it and stores it on the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with patch(
        "custom_components.rohlikcz.config_flow.validate_input", return_value=VALID
    ) as mock_validate, patch(
        "custom_components.rohlikcz.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_SITE: "de"}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {**USER_INPUT, CONF_SITE: "de"}
    assert mock_validate.call_args.args[1][CONF_SITE] == "de"


async def test_validate_input_uses_site_base_url(hass: HomeAssistant) -> None:
    """The login check goes to the selected shop, and to Rohlík.cz by default."""
    with patch("custom_components.rohlikcz.config_flow.RohlikAPI") as mock_api:
        client = mock_api.return_value
        client.login = AsyncMock(
            return_value={"data": {"user": {"name": "Test User", "id": 123456}}}
        )
        client.close = AsyncMock()

        assert await validate_input(hass, {**USER_INPUT, CONF_SITE: "hu"}) == VALID
        assert mock_api.call_args.kwargs["base_url"] == "https://www.kifli.hu"

        await validate_input(hass, USER_INPUT)
        assert mock_api.call_args.kwargs["base_url"] == "https://www.rohlik.cz"


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


async def test_user_flow_cannot_connect(hass: HomeAssistant) -> None:
    """A network/API error surfaces a cannot_connect error on the form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.rohlikcz.config_flow.validate_input",
        side_effect=RohlikAPIError("rohlik down"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


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


async def test_reauth_keeps_site(hass: HomeAssistant) -> None:
    """Reauth logs in to the entry's own shop and keeps it on the entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "old", CONF_SITE: "at"},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with patch(
        "custom_components.rohlikcz.config_flow.validate_input", return_value=VALID
    ) as mock_validate, patch(
        "custom_components.rohlikcz.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )

    assert result["reason"] == "reauth_successful"
    assert mock_validate.call_args.args[1][CONF_SITE] == "at"
    assert entry.data[CONF_SITE] == "at"


async def test_reconfigure_moves_entry_to_other_site(hass: HomeAssistant) -> None:
    """An entry from before site selection can be switched to Knuspr.de."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "old"},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    with patch(
        "custom_components.rohlikcz.config_flow.validate_input", return_value=VALID
    ) as mock_validate, patch(
        "custom_components.rohlikcz.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SITE: "de", CONF_PASSWORD: "new-password"}
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_validate.call_args.args[1][CONF_SITE] == "de"
    assert entry.data == {
        CONF_EMAIL: "test@example.com",
        CONF_PASSWORD: "new-password",
        CONF_SITE: "de",
    }


async def test_reconfigure_invalid_auth(hass: HomeAssistant) -> None:
    """Credentials the chosen shop rejects keep the form open."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "old"},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with patch(
        "custom_components.rohlikcz.config_flow.validate_input",
        side_effect=InvalidCredentialsError("bad"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SITE: "de", CONF_PASSWORD: "wrong"}
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert CONF_SITE not in entry.data


async def test_reconfigure_wrong_account(hass: HomeAssistant) -> None:
    """Reconfiguring onto a different account aborts with wrong_account."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "old"},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with patch(
        "custom_components.rohlikcz.config_flow.validate_input",
        return_value={"title": "Other", "user_id": "999999"},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SITE: "de", CONF_PASSWORD: "new-password"}
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "wrong_account"


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
