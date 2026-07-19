"""Config flow for Zendure Integration integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .api import Api, ApiError
from .const import (
    CONF_APPTOKEN,
    CONF_AUTO_MQTT_USER,
    CONF_DEVICE_IP,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    CONF_DEVICE_SN,
    CONF_DEVICES,
    CONF_LOCAL_ONLY,
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
from .device import ZendureZenSdk
from .manager import ZendureConfigEntry

_LOGGER = logging.getLogger(__name__)


class ZendureConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Zendure Integration."""

    VERSION = 1
    MINOR_VERSION = 8
    _input_data: dict[str, Any]
    data_schema = vol.Schema(
        {
            vol.Required(CONF_APPTOKEN): str,
            vol.Required(CONF_P1METER, description={"suggested_value": "sensor.power_actual"}): selector.EntitySelector(),
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
            vol.Optional(CONF_AUTO_MQTT_USER, default=False): bool,
            vol.Optional(CONF_WIFISSID): str,
            vol.Optional(CONF_WIFIPSW): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD,
                ),
            ),
        }
    )
    # Per-device fields only; the entry-level settings (P1 meter, logging) are asked
    # once at the end of the add-loop, not per device.
    localsdk_device_schema = vol.Schema(
        {
            vol.Required(CONF_DEVICE_IP): str,
            vol.Optional(CONF_DEVICE_NAME): str,
        }
    )
    localsdk_settings_schema = vol.Schema(
        {
            vol.Required(CONF_P1METER, description={"suggested_value": "sensor.power_actual"}): selector.EntitySelector(),
            vol.Required(CONF_MQTTLOG, default=False): bool,
        }
    )

    def __init__(self) -> None:
        """Initialize."""
        self._user_input: dict[str, Any] = {}
        # Local (cloud-free) devices accumulated across the add-loop / reconfigure.
        self._devices: list[dict[str, Any]] = []
        self._devices_loaded = False

    async def async_step_user(self, _user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Let the user choose between cloud and local (cloud-free) setup."""
        # Only one Zendure entry is supported; abort up front so the user does not
        # walk the whole (possibly multi-device) flow only to be rejected at the end.
        await self.async_set_unique_id("Zendure", raise_on_progress=False)
        self._abort_if_unique_id_configured()
        return self.async_show_menu(step_id="user", menu_options=["cloud", "localsdk"])

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step for the cloud (token based) setup."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            self._user_input = user_input

            try:
                await Api.Connect(self.hass, self._user_input, False)
            except ApiError as err:
                errors["base"] = err.key
                placeholders["detail"] = err.detail
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.error("Unexpected exception: %s", err)
                errors["base"] = "unknown"
            else:
                localmqtt = user_input[CONF_MQTTLOCAL]
                if localmqtt:
                    return await self.async_step_local()

                await self.async_set_unique_id("Zendure", raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Zendure", data=self._user_input)

        return self.async_show_form(step_id="cloud", data_schema=self.data_schema, errors=errors, description_placeholders=placeholders)

    @staticmethod
    def _resolve_model(product: str) -> str | None:
        """Return the ZenSDK model key auto-detected from the device's reported product string."""
        # Only accept models whose device class is a ZendureZenSdk subclass: a
        # legacy/cloud-only model here would build a device class that ignores
        # definition["local"] and never comes online.
        key = str(product).lower().replace(" ", "")
        cls = Api.createdevice.get(key)
        return key if cls is not None and issubclass(cls, ZendureZenSdk) else None

    async def _validate_device(self, user_input: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, str], dict[str, str]]:
        """Reach the device, resolve its model, and return a device dict (or errors)."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        ip = str(user_input[CONF_DEVICE_IP]).strip()
        try:
            sn, product = await Api.testLocalDevice(self.hass, ip)
        except ApiError as err:
            errors["base"] = err.key
            placeholders["detail"] = err.detail
            return None, errors, placeholders
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error("Unexpected exception: %s", err)
            errors["base"] = "unknown"
            return None, errors, placeholders

        model = self._resolve_model(product)
        if model is None:
            errors["base"] = "unsupported_model"
            placeholders["detail"] = product or "unknown"
            return None, errors, placeholders

        if any(str(d.get(CONF_DEVICE_SN)) == sn for d in self._devices):
            errors["base"] = "duplicate_device"
            placeholders["detail"] = sn
            return None, errors, placeholders

        # Keep the device name unique: first append the serial suffix, then a
        # counter, so a secondary collision cannot produce two identical names.
        base = str(user_input.get(CONF_DEVICE_NAME, "") or "").strip() or model
        existing = {str(d.get(CONF_DEVICE_NAME)) for d in self._devices}
        name = base
        if name in existing:
            name = f"{base} {sn[-4:]}"
            i = 2
            while name in existing:
                name = f"{base} {sn[-4:]} ({i})"
                i += 1

        return {CONF_DEVICE_IP: ip, CONF_DEVICE_SN: sn, CONF_DEVICE_MODEL: model, CONF_DEVICE_NAME: name}, errors, placeholders

    async def async_step_localsdk(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add one local (cloud-free) device via its on-device Zen SDK HTTP API."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            device, errors, placeholders = await self._validate_device(user_input)
            if device is not None:
                self._devices.append(device)
                return await self.async_step_localsdk_menu()

        return self.async_show_form(step_id="localsdk", data_schema=self.localsdk_device_schema, errors=errors, description_placeholders=placeholders)

    async def async_step_localsdk_menu(self, _user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Offer to add another local device or finish the setup."""
        return self.async_show_menu(
            step_id="localsdk_menu",
            menu_options=["localsdk", "localsdk_settings"],
            description_placeholders={
                "count": str(len(self._devices)),
                "devices": ", ".join(str(d.get(CONF_DEVICE_NAME, "")) for d in self._devices),
            },
        )

    async def async_step_localsdk_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask the entry-level settings once and create the local config entry."""
        if user_input is not None:
            data = {
                CONF_LOCAL_ONLY: True,
                CONF_DEVICES: self._devices,
                CONF_P1METER: user_input[CONF_P1METER],
                CONF_MQTTLOG: user_input[CONF_MQTTLOG],
            }
            await self.async_set_unique_id("Zendure", raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Zendure", data=data)

        return self.async_show_form(step_id="localsdk_settings", data_schema=self.localsdk_settings_schema)

    async def async_step_local(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None and user_input.get(CONF_MQTTSERVER, None) is not None:
            try:
                self._user_input = self._user_input | user_input if self._user_input else user_input
                await Api.Connect(self.hass, self._user_input, False)
            except ApiError as err:
                errors["base"] = err.key
                placeholders["detail"] = err.detail
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.error("Unexpected exception: %s", err)
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id("Zendure", raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Zendure", data=self._user_input)

        return self.async_show_form(step_id="local", data_schema=self.mqtt_schema, errors=errors, description_placeholders=placeholders)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add reconfigure step to allow to reconfigure a config entry."""
        errors: dict[str, str] = {}

        entry = self._get_reconfigure_entry()
        if entry.data.get(CONF_LOCAL_ONLY):
            return await self.async_step_reconfigure_local(user_input)
        schema = self.data_schema
        placeholders: dict[str, str] = {}
        if user_input is not None:
            self._user_input = self._user_input | user_input
            use_mqtt = user_input.get(CONF_MQTTLOCAL, False)
            if use_mqtt:
                schema = self.mqtt_schema
            else:
                try:
                    await Api.Connect(self.hass, self._user_input, False)
                except ApiError as err:
                    errors["base"] = err.key
                    placeholders["detail"] = err.detail
                except Exception as err:  # pylint: disable=broad-except
                    _LOGGER.error("Unexpected exception: %s", err)
                    errors["base"] = "unknown"
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
            description_placeholders=placeholders,
        )

    def _load_local_devices(self) -> None:
        """Seed the working device list from the entry the first time we enter reconfigure."""
        if self._devices_loaded:
            return
        entry = self._get_reconfigure_entry()
        self._devices = [dict(d) for d in entry.data.get(CONF_DEVICES, [])]
        self._devices_loaded = True

    async def async_step_reconfigure_local(self, _user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the local device list: add, remove, or save and reload."""
        self._load_local_devices()
        return self.async_show_menu(
            step_id="reconfigure_local",
            menu_options=["reconfig_add", "reconfig_remove", "reconfig_save"],
            description_placeholders={
                "count": str(len(self._devices)),
                "devices": ", ".join(str(d.get(CONF_DEVICE_NAME, "")) for d in self._devices),
            },
        )

    async def async_step_reconfig_add(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add a device to an existing local config entry."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            device, errors, placeholders = await self._validate_device(user_input)
            if device is not None:
                self._devices.append(device)
                return await self.async_step_reconfigure_local()

        return self.async_show_form(step_id="reconfig_add", data_schema=self.localsdk_device_schema, errors=errors, description_placeholders=placeholders)

    async def async_step_reconfig_remove(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Remove one or more devices from an existing local config entry."""
        if not self._devices:
            return await self.async_step_reconfigure_local()

        if user_input is not None:
            remove = set(user_input.get("remove", []))
            self._devices = [d for d in self._devices if str(d.get(CONF_DEVICE_SN)) not in remove]
            return await self.async_step_reconfigure_local()

        options = [selector.SelectOptionDict(value=str(d.get(CONF_DEVICE_SN)), label=str(d.get(CONF_DEVICE_NAME))) for d in self._devices]
        schema = vol.Schema(
            {
                vol.Required("remove"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, multiple=True, mode=selector.SelectSelectorMode.LIST),
                ),
            }
        )
        return self.async_show_form(step_id="reconfig_remove", data_schema=schema)

    async def async_step_reconfig_save(self, _user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Persist the edited device list and reload the entry."""
        entry = self._get_reconfigure_entry()
        if not self._devices:
            return self.async_abort(reason="no_devices_configured")

        # Drop the legacy single-device keys so CONF_DEVICES is the single source of truth.
        data = {k: v for k, v in entry.data.items() if k not in (CONF_DEVICE_IP, CONF_DEVICE_SN, CONF_DEVICE_MODEL, CONF_DEVICE_NAME)}
        data[CONF_LOCAL_ONLY] = True
        data[CONF_DEVICES] = self._devices
        await self.async_set_unique_id("Zendure", raise_on_progress=False)
        self._abort_if_unique_id_mismatch()
        return self.async_update_reload_and_abort(entry, data=data)

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
                vol.Optional(CONF_AUTO_MQTT_USER, default=self.config_entry.data.get(CONF_AUTO_MQTT_USER, False)): bool,
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
