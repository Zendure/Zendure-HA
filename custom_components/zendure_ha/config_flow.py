"""Config flow for Zendure Integration integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .api import Api
from .const import (
    CONF_APPTOKEN,
    CONF_MQTTLOCAL,
    CONF_MQTTLOG,
    CONF_MQTTPORT,
    CONF_MQTTPSW,
    CONF_MQTTSERVER,
    CONF_MQTTUSER,
    CONF_P1METER,
    CONF_SIM,
    CONF_WIFIPSW,
    CONF_WIFISSID,
    DOMAIN,
)
from .manager import ZendureConfigEntry

_LOGGER = logging.getLogger(__name__)


class ZendureConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Zendure Integration."""

    VERSION = 1
    _input_data: dict[str, Any]
    data_schema = vol.Schema(
        {
            vol.Required(CONF_APPTOKEN): str,
            vol.Required(CONF_P1METER, description={"suggested_value": "sensor.power_actual"}): str,
            vol.Required(CONF_MQTTLOG): bool,
            vol.Required(CONF_MQTTLOCAL): bool,
        }
    )
    mqtt_schema = vol.Schema(
        {
            vol.Required(CONF_MQTTSERVER): str,
            vol.Required(CONF_MQTTPORT, default=1883): int,
            vol.Required(CONF_MQTTUSER): str,
            vol.Optional(CONF_MQTTPSW): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD,
                ),
            ),
            vol.Optional(CONF_WIFISSID): str,
            vol.Optional(CONF_WIFIPSW): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD,
                ),
            ),
        }
    )

    def __init__(self) -> None:
        """Initialize."""
        self._user_input: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step when user initializes a integration."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._user_input = user_input

            try:
                if await Api.Connect(self.hass, self._user_input, False) is None:
                    errors["base"] = "invalid input"
                else:
                    localmqtt = user_input[CONF_MQTTLOCAL]
                    if localmqtt:
                        return await self.async_step_local()

                    await self.async_set_unique_id("Zendure", raise_on_progress=False)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title="Zendure", data=self._user_input)

            except Exception as err:  # pylint: disable=broad-except
                errors["base"] = f"invalid input {err}"

        return self.async_show_form(step_id="user", data_schema=self.data_schema, errors=errors)

    async def async_step_local(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and user_input.get(CONF_MQTTSERVER, None) is not None:
            try:
                self._user_input = self._user_input | user_input if self._user_input else user_input
                if await Api.Connect(self.hass, self._user_input, False) is None:
                    errors["base"] = "invalid input"
            except Exception as err:  # pylint: disable=broad-except
                errors["base"] = f"invalid input {err}"
            else:
                await self.async_set_unique_id("Zendure", raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Zendure", data=self._user_input)

        return self.async_show_form(step_id="local", data_schema=self.mqtt_schema, errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add reconfigure step to allow to reconfigure a config entry."""
        errors: dict[str, str] = {}

        entry = self._get_reconfigure_entry()
        schema = self.data_schema
        if user_input is not None:
            self._user_input = self._user_input | user_input
        if user_input is not None:
            use_mqtt = user_input.get(CONF_MQTTLOCAL, False)
            if use_mqtt:
                schema = self.mqtt_schema
            else:
                try:
                    if await Api.Connect(self.hass, self._user_input, False) is None:
                        errors["base"] = "invalid input"
                except Exception as err:  # pylint: disable=broad-except
                    _LOGGER.error(f"Unexpected exception: {err}")
                    errors["base"] = f"invalid input {err}"
                else:
                    await self.async_set_unique_id("Zendure", raise_on_progress=False)
                    self._abort_if_unique_id_mismatch()

                    return self.async_update_reload_and_abort(entry, data=self._user_input)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                data_schema=schema,
                suggested_values=entry.data | (user_input or {}),
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(_config_entry: ZendureConfigEntry) -> ZendureOptionsFlowHandler:
        """Get the options flow for this handler."""
        return ZendureOptionsFlowHandler()


class ZendureOptionsFlowHandler(OptionsFlow):
    """Handles the options flow."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle options flow."""
        if user_input is not None:
            data = self.config_entry.data | user_input
            self.hass.config_entries.async_update_entry(self.config_entry, data=data)
            return self.async_create_entry(title="", data=data)

        options_schema = vol.Schema(
            {
                vol.Required(CONF_P1METER, default=self.config_entry.data[CONF_P1METER]): str,
                vol.Required(CONF_MQTTLOG, default=self.config_entry.data[CONF_MQTTLOG]): bool,
                vol.Optional(CONF_SIM, default=self.config_entry.data.get(CONF_SIM, False)): bool,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(options_schema, self.config_entry.data),
        )


class ZendureConnectionError(HomeAssistantError):
    """Error to indicate there is a connection issue with Zendure Integration."""

    def __init__(self) -> None:
        """Initialize the connection error."""
        super().__init__("Zendure Integration")
