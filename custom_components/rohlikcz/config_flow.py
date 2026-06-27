import logging
from typing import Any, Mapping

from homeassistant.const import CONF_PASSWORD, CONF_EMAIL
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
import voluptuous as vol

from .const import (
    DOMAIN, CONF_ANALYTICS, ANALYTICS_OPTIONS, DEFAULT_ANALYTICS,
    CONF_TOP_N, DEFAULT_TOP_N, CONF_HIDE_DISCONTINUED, DEFAULT_HIDE_DISCONTINUED,
)
from rohlik_api import InvalidCredentialsError, RohlikAPI

_LOGGER = logging.getLogger(__name__)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
    """Validate the user input allows us to connect.

    Returns the account title and unique user id on success.
    """
    # A one-shot client that owns (and on close fully tears down) its session.
    client = RohlikAPI(data[CONF_EMAIL], data[CONF_PASSWORD])
    try:
        reply = await client.login()
        user = reply["data"]["user"]
        return {"title": user["name"], "user_id": str(user["id"])}
    finally:
        await client.close()


ANALYTICS_SCHEMA = vol.Schema({
    vol.Optional(CONF_ANALYTICS, default=DEFAULT_ANALYTICS): SelectSelector(
        SelectSelectorConfig(
            options=ANALYTICS_OPTIONS,
            multiple=True,
            mode=SelectSelectorMode.LIST,
            translation_key=CONF_ANALYTICS,
        )
    ),
    vol.Optional(CONF_TOP_N, default=DEFAULT_TOP_N): NumberSelector(
        NumberSelectorConfig(
            min=5,
            max=200,
            step=5,
            mode=NumberSelectorMode.BOX,
        )
    ),
    vol.Optional(CONF_HIDE_DISCONTINUED, default=DEFAULT_HIDE_DISCONTINUED): BooleanSelector(),
})


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._user_title: str | None = None
        self._user_data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:

        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except InvalidCredentialsError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unknown exception")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info["user_id"])
                self._abort_if_unique_id_configured()
                self._user_title = info["title"]
                self._user_data = user_input
                return await self.async_step_analytics()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
        )

    async def async_step_analytics(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Second step: choose analytics levels."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._user_title,
                data=self._user_data,
                options={
                    CONF_ANALYTICS: user_input.get(CONF_ANALYTICS, []),
                    CONF_TOP_N: int(user_input.get(CONF_TOP_N, DEFAULT_TOP_N)),
                    CONF_HIDE_DISCONTINUED: user_input.get(CONF_HIDE_DISCONTINUED, DEFAULT_HIDE_DISCONTINUED),
                },
            )

        return self.async_show_form(
            step_id="analytics",
            data_schema=ANALYTICS_SCHEMA,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when credentials become invalid."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm re-authentication by re-entering the password."""
        reauth_entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            data = {
                CONF_EMAIL: reauth_entry.data[CONF_EMAIL],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            try:
                info = await validate_input(self.hass, data)
            except InvalidCredentialsError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unknown exception")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info["user_id"])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(reauth_entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"email": reauth_entry.data[CONF_EMAIL]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return RohlikOptionsFlowHandler()


class RohlikOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for existing entries (reconfigure analytics)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            user_input[CONF_TOP_N] = int(user_input.get(CONF_TOP_N, DEFAULT_TOP_N))
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(CONF_ANALYTICS, DEFAULT_ANALYTICS)
        current_top_n = self.config_entry.options.get(CONF_TOP_N, DEFAULT_TOP_N)
        current_hide = self.config_entry.options.get(CONF_HIDE_DISCONTINUED, DEFAULT_HIDE_DISCONTINUED)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_ANALYTICS, default=current): SelectSelector(
                    SelectSelectorConfig(
                        options=ANALYTICS_OPTIONS,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                        translation_key=CONF_ANALYTICS,
                    )
                ),
                vol.Optional(CONF_TOP_N, default=current_top_n): NumberSelector(
                    NumberSelectorConfig(
                        min=5,
                        max=200,
                        step=5,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(CONF_HIDE_DISCONTINUED, default=current_hide): BooleanSelector(),
            }),
        )
