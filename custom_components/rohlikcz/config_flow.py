import logging
from typing import Any

from homeassistant.const import CONF_PASSWORD, CONF_EMAIL
from homeassistant import config_entries
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
from .errors import InvalidCredentialsError
from .rohlik_api import RohlikCZAPI

_LOGGER = logging.getLogger(__name__)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Validate the user input allows us to connect."""
    api = RohlikCZAPI(data[CONF_EMAIL], data[CONF_PASSWORD])
    reply = await api.get_data()
    title: str = reply["login"]["data"]["user"]["name"]
    return title, data


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

    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL
    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._user_title: str | None = None
        self._user_data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:

        data_schema: dict[Any, Any] = {
            vol.Required(CONF_EMAIL, default="e-mail"): str,
            vol.Required(CONF_PASSWORD, default="password"): str,
        }

        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info, data = await validate_input(self.hass, user_input)
                self._user_title = info
                self._user_data = data
                return await self.async_step_analytics()

            except InvalidCredentialsError:
                errors["base"] = "invalid_auth"

            except Exception:
                _LOGGER.exception("Unknown exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(data_schema), errors=errors
        )

    async def async_step_analytics(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return RohlikOptionsFlowHandler()


class RohlikOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for existing entries (reconfigure analytics)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
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
