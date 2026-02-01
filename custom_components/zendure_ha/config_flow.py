"""Config flow for Zendure Integration integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import mqtt
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH

from .const import (
    CONF_APPTOKEN,
    CONF_P1METER,
    CONF_WIFIPSW,
    CONF_WIFISSID,
    DOMAIN,
)
from .coordinator import ZendureConfigEntry

_LOGGER = logging.getLogger(__name__)


class ZendureConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Zendure Integration."""

    VERSION = 1
    _input_data: dict[str, Any]
    data_schema = vol.Schema(
        {
            vol.Optional(CONF_APPTOKEN): str,
            vol.Required(CONF_P1METER, description={"suggested_value": "sensor.power_actual"}): selector.EntitySelector(),
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
        await self.async_set_unique_id("Zendure", raise_on_progress=False)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            self._user_input = user_input

            try:
                try:
                    if not mqtt.is_connected(self.hass):
                        return self.async_abort(reason="mqtt_not_connected")
                except KeyError:
                    return self.async_abort(reason="mqtt_not_configured")

                await self.async_set_unique_id("Zendure", raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Zendure", data=self._user_input)

            except Exception as err:  # pylint: disable=broad-except
                errors["base"] = f"invalid input {err}"

        return self.async_show_form(step_id="user", data_schema=self.data_schema, errors=errors)

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

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        """Handle a discovered Bluetooth device."""
        if len(entries := self._async_current_entries()) == 0 or discovery_info.manufacturer_id is None:
            return self.async_abort(reason="single_instance_allowed")

        sn = discovery_info.manufacturer_data.get(discovery_info.manufacturer_id, b"").decode("utf8")[:-1]
        device_registry = dr.async_get(self.hass)
        for d in dr.async_entries_for_config_entry(device_registry, entries[0].entry_id):
            if d.serial_number and d.serial_number.endswith(sn):
                device_registry.async_update_device(d.id, merge_connections={(CONNECTION_BLUETOOTH, discovery_info.address)})
                return self.async_abort(reason="already_configured")

        await Api.IotToHA(discovery_info)
        return self.async_abort(reason="unknown")

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
            return self.async_create_entry(title="Zendure", data=data)

        options_schema = vol.Schema(
            {
                vol.Required(CONF_P1METER, default=self.config_entry.data[CONF_P1METER]): str,
                vol.Optional(CONF_WIFISSID): str,
                vol.Optional(CONF_WIFIPSW): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.PASSWORD,
                    ),
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(options_schema, self.config_entry.data),
        )
