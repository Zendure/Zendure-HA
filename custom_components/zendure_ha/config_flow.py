"""Config flow for Zendure Integration integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .api import Api, find_zensdk_sn
from .const import (
    CONF_APPTOKEN,
    CONF_AUTO_MQTT_SETUP,
    CONF_AUTO_MQTT_USER,
    CONF_DEVICE_IP,
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
    MINOR_VERSION = 8
    _input_data: dict[str, Any]
    data_schema = vol.Schema(
        {
            vol.Optional(CONF_APPTOKEN, description={"suggested_value": ""}): str,
            vol.Required(
                CONF_P1METER, description={"suggested_value": "sensor.power_actual"}
            ): selector.EntitySelector(),
            vol.Required(CONF_MQTTLOG): bool,
            vol.Required(CONF_MQTTLOCAL): bool,
            vol.Optional(CONF_DEVICE_IP, description={"suggested_value": ""}): str,
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
            vol.Optional(CONF_AUTO_MQTT_USER, default=False): bool,
            vol.Optional(CONF_AUTO_MQTT_SETUP, default=False): bool,
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
        self._discovered: dict[str, str] = {}
        self._connect_task: Any = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step when user initializes a integration."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._user_input = self._user_input | user_input

            try:
                token = user_input.get(CONF_APPTOKEN, "")
                device_ip = user_input.get(CONF_DEVICE_IP, "")
                token_free = not token or len(token) <= 1

                # Token-free with device_ip: validate local device before creating entry
                if token_free and device_ip:
                    if await Api.Connect(self.hass, self._user_input, False) is None:
                        errors["base"] = "invalid input"
                    elif user_input.get(CONF_MQTTLOCAL, False):
                        return await self.async_step_local()
                    else:
                        await self.async_set_unique_id(
                            "Zendure", raise_on_progress=False
                        )
                        self._abort_if_unique_id_configured()
                        return self.async_create_entry(
                            title="Zendure", data=self._user_input
                        )
                elif await Api.Connect(self.hass, self._user_input, False) is None:
                    errors["base"] = "invalid input"
                else:
                    localmqtt = user_input[CONF_MQTTLOCAL]
                    if localmqtt:
                        return await self.async_step_local()

                    await self.async_set_unique_id("Zendure", raise_on_progress=False)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title="Zendure", data=self._user_input
                    )

            except Exception as err:  # pylint: disable=broad-except
                errors["base"] = f"invalid input {err}"

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                self.data_schema, self._user_input
            ),
            errors=errors,
        )

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and user_input.get(CONF_MQTTSERVER, None) is not None:
            try:
                self._user_input = (
                    self._user_input | user_input if self._user_input else user_input
                )
                devices = await Api.Connect(self.hass, self._user_input, False)
                if devices is None:
                    errors["base"] = "invalid input"
                elif user_input.get(CONF_AUTO_MQTT_SETUP, False):
                    device_ip = self._user_input.get(CONF_DEVICE_IP, "")
                    device_list = devices.get("deviceList", [])
                    sn = find_zensdk_sn(device_list, device_ip)
                    if not device_ip or not sn:
                        errors["base"] = "mqtt_setup_missing_device"
                    else:
                        success = await Api.ZenSdkMqttSetup(
                            self.hass,
                            device_ip,
                            sn,
                            user_input[CONF_MQTTSERVER],
                            user_input[CONF_MQTTPORT],
                            user_input.get(CONF_MQTTUSER, ""),
                            user_input.get(CONF_MQTTPSW, ""),
                        )
                        if not success:
                            errors["base"] = "mqtt_setup_failed"
            except Exception as err:  # pylint: disable=broad-except
                errors["base"] = f"invalid input {err}"

            if not errors:
                await self.async_set_unique_id("Zendure", raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Zendure", data=self._user_input)

        return self.async_show_form(
            step_id="local", data_schema=self.mqtt_schema, errors=errors
        )

    async def async_step_zeroconf(self, discovery_info: Any) -> ConfigFlowResult:
        """Handle mDNS discovery — called by HA when a Zendure device is found."""
        host = discovery_info.host
        # Strip service suffix — works for _http._tcp.local. and _zendure._tcp.local.
        raw_name = discovery_info.name.split("._")[0]
        # Extract SN: last segment matching a serial (uppercase alphanum, ≥8 chars).
        # Falls back to the last dash-segment so unknown models don't break the flow.
        match = re.search(r"-([A-Z0-9]{8,})$", raw_name)
        sn = match.group(1) if match else raw_name.split("-")[-1]

        await self.async_set_unique_id("Zendure")
        self._abort_if_unique_id_configured(updates={CONF_DEVICE_IP: host})

        # Query the device directly to confirm SN and get the human-readable model name.
        # This is a lightweight call — no cloud auth required.
        device_info = await Api.LocalDiscovery(self.hass, host)
        if device_info:
            device = device_info.get("deviceList", [{}])[0]
            sn = device.get("snNumber", sn)
            model = device.get("productModel", "")
        else:
            model = ""

        self._discovered = {"device_ip": host, "sn": sn, "model": model}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show confirmation form with pre-filled IP; user may override."""
        if user_input is not None:
            ip = user_input[CONF_DEVICE_IP]
            self._user_input[CONF_DEVICE_IP] = ip
            self._discovered["device_ip"] = ip
            self._connect_task = None
            return await self.async_step_zeroconf_connect()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DEVICE_IP, default=self._discovered.get("device_ip", "")
                ): str,
            }
        )
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=schema,
            description_placeholders=self._discovered,
        )

    async def async_step_zeroconf_connect(
        self, _user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show a progress spinner while connecting to the device."""
        if self._connect_task is None:
            self._connect_task = self.hass.async_create_task(
                Api.Connect(self.hass, self._user_input, False)
            )

        if not self._connect_task.done():
            return self.async_show_progress(
                step_id="zeroconf_connect",
                progress_action="connecting",
                progress_task=self._connect_task,
            )

        try:
            result = self._connect_task.result()
        except Exception as err:
            _LOGGER.error("Connection error during zeroconf setup: %s", err)
            result = None

        self._connect_task = None

        if result is None:
            return self.async_show_progress_done(next_step_id="zeroconf_failed")

        return self.async_show_progress_done(next_step_id="user")

    async def async_step_zeroconf_failed(
        self, _user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Bounce back to the confirm form with a connection error."""
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DEVICE_IP, default=self._discovered.get("device_ip", "")
                ): str,
            }
        )
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=schema,
            description_placeholders=self._discovered,
            errors={"base": "cannot_connect"},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add reconfigure step to allow to reconfigure a config entry."""
        errors: dict[str, str] = {}

        entry = self._get_reconfigure_entry()
        schema = self.data_schema
        if user_input is not None:
            self._user_input = self._user_input | user_input
            use_mqtt = user_input.get(CONF_MQTTLOCAL, False)
            if use_mqtt:
                schema = self.mqtt_schema
            else:
                try:
                    if await Api.Connect(self.hass, self._user_input, False) is None:
                        errors["base"] = "invalid input"
                except Exception as err:  # pylint: disable=broad-except
                    _LOGGER.error("Unexpected exception: %s", err)
                    errors["base"] = f"invalid input {err}"
                else:
                    await self.async_set_unique_id("Zendure", raise_on_progress=False)
                    self._abort_if_unique_id_mismatch()

                    return self.async_update_reload_and_abort(
                        entry, data=self._user_input
                    )

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
    def async_get_options_flow(
        _config_entry: ZendureConfigEntry,
    ) -> ZendureOptionsFlowHandler:
        """Get the options flow for this handler."""
        return ZendureOptionsFlowHandler()


class ZendureOptionsFlowHandler(OptionsFlow):
    """Handles the options flow."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options flow."""
        if user_input is not None:
            data = self.config_entry.data | user_input
            self.hass.config_entries.async_update_entry(self.config_entry, data=data)
            return self.async_create_entry(title="", data=data)

        options_schema = vol.Schema(
            {
                vol.Required(
                    CONF_P1METER, default=self.config_entry.data[CONF_P1METER]
                ): str,
                vol.Required(
                    CONF_MQTTLOG, default=self.config_entry.data[CONF_MQTTLOG]
                ): bool,
                vol.Optional(
                    CONF_AUTO_MQTT_USER,
                    default=self.config_entry.data.get(CONF_AUTO_MQTT_USER, False),
                ): bool,
                vol.Optional(
                    CONF_SIM, default=self.config_entry.data.get(CONF_SIM, False)
                ): bool,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                options_schema, self.config_entry.data
            ),
        )


class ZendureConnectionError(HomeAssistantError):
    """Error to indicate there is a connection issue with Zendure Integration."""

    def __init__(self) -> None:
        """Initialize the connection error."""
        super().__init__("Zendure Integration")
