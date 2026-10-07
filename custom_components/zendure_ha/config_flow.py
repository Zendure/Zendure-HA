"""Config flow: one instance with its settings in sections, and a subentry per device the driver finds."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, ConfigSubentryData, ConfigSubentryFlow, SubentryFlowResult
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import selector

from .const import DOMAIN, PANEL_TITLE
from .entity import unique_name
from .zendure.driver import ZendureDriver

DISTRIBUTION = vol.Schema({
    vol.Required("max_kw"): selector({"number": {"min": 0, "step": 0.1, "unit_of_measurement": "kW"}}),
    vol.Optional("max_output"): vol.All(selector({"number": {"min": 0, "step": 100, "unit_of_measurement": "W"}}), vol.Coerce(int)),
    vol.Optional("p1_device"): selector({"device": {}}),
    vol.Optional("p1_entity"): selector({"entity": {"domain": "sensor"}}),
    vol.Required("mqtt_log"): selector({"boolean": {}}),
})
CLOUD = vol.Schema({vol.Optional("apptoken"): selector({"text": {"type": "password"}}), vol.Optional("cloud_url"): selector({"text": {}})})
LOCAL_BROKER = vol.Schema({vol.Optional("mqtt_host"): selector({"text": {}}), vol.Optional("mqtt_port"): vol.All(selector({"number": {"min": 1, "max": 65535, "mode": "box"}}), vol.Coerce(int)), vol.Optional("mqtt_user"): selector({"text": {}}), vol.Optional("mqtt_psw"): selector({"text": {"type": "password"}})})
WIFI = vol.Schema({vol.Optional("wifi_ssid"): selector({"text": {}}), vol.Optional("wifi_psw"): selector({"text": {"type": "password"}})})
ZENDURE = {"cloud": CLOUD, "local_broker": LOCAL_BROKER, "wifi": WIFI}  # the form sections that together make the "zendure" entry section
SETTINGS_SCHEMA = vol.Schema({vol.Required("distribution"): section(DISTRIBUTION), **{vol.Required(name): section(schema, {"collapsed": True}) for name, schema in ZENDURE.items()}})
DEVICE = vol.Schema({vol.Optional("ip"): selector({"text": {}}), vol.Optional("blemac"): selector({"text": {}})})
DEVICE_ADD = vol.Schema({vol.Required("sn"): str, vol.Required("product"): str, vol.Optional("device_id"): str, vol.Optional("prodkey"): str, **DEVICE.schema})  # a manual add asks for the identity as well
PANEL = vol.Schema({vol.Optional("power_entity"): selector({"entity": {"domain": "sensor"}}), vol.Optional("derate_entity"): selector({"entity": {"domain": "number"}})})
PANEL_ADD = vol.Schema({vol.Required("name"): str, **PANEL.schema})  # a manual add names the group as well


def nested(data: dict[str, dict]) -> dict:
    # entry data into the form sections, for the suggested values
    zendure = data.get(ZendureDriver.id, {})
    return {"distribution": data.get("distribution", {}), **{name: values for name, schema in ZENDURE.items() if (values := {k: v for k, v in zendure.items() if k in schema.schema})}}


def flat(user_input: dict) -> dict[str, dict]:
    # the form sections back to the two entry sections
    return {"distribution": user_input["distribution"], ZendureDriver.id: {k: v for name in ZENDURE for k, v in user_input.get(name, {}).items()}}


class ZendureConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance flow: the settings form, and discovery that either seeds it or feeds the running coordinator."""

    VERSION = 2

    def __init__(self) -> None:
        self._zeroconf: dict[str, str] = {}  # serial -> ip found before the entry existed; the coordinator picks it up
        self._found: list[ConfigSubentryData] = []  # devices identified over their local API before the entry existed

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(PANEL_TITLE)
        self._abort_if_unique_id_configured()
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(SETTINGS_SCHEMA, nested({"distribution": {"max_kw": 17.0, "mqtt_log": False}, "zendure": {"mqtt_port": 1883}})))
        distribution = ConfigSubentryData(data={}, subentry_type="distribution", title="Distribution", unique_id="distribution")  # the fleet control gets its own card
        return self.async_create_entry(title=PANEL_TITLE, data={**flat(user_input), "zeroconf": self._zeroconf}, subentries=[distribution, *self._found])

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is None:
            return self.async_show_form(step_id="reconfigure", data_schema=self.add_suggested_values_to_schema(SETTINGS_SCHEMA, nested(dict(entry.data))))
        return self.async_update_reload_and_abort(entry, data={**entry.data, **flat(user_input)})

    # Discovery only reaches these steps before the entry exists: HA aborts discovery flows of a single-instance integration
    # itself once it is set up. From then on the driver listens for mDNS and Bluetooth itself.

    def _add_found(self, found: dict[str, Any] | None) -> None:
        # a device that told us who it is becomes a subentry of the entry about to be created
        if found is None or (family := ZendureDriver.device_family(found)) is None or any(s["data"]["sn"] == found["sn"] for s in self._found):
            return
        title = unique_name(family, found["sn"], {s["title"] for s in self._found})
        self._found.append(ConfigSubentryData(data={"ip": "", "blemac": "", **found}, subentry_type=ZendureDriver.id, title=title, unique_id=f"{ZendureDriver.id}_{found['sn']}"))

    async def async_step_zeroconf(self, discovery_info: Any) -> ConfigFlowResult:
        # the service is asked who it is over its local API; without an answer its ip waits in the entry until the device arrives
        if (match := ZendureDriver.match_zeroconf(discovery_info)) is None:
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(PANEL_TITLE)
        self._abort_if_unique_id_configured()
        if (found := await ZendureDriver.probe(self.hass, match["ip"])) is not None:
            self._add_found(found)
        else:
            self._zeroconf[match["sn"]] = match["ip"]
        self.context["title_placeholders"] = {"name": f"{match['model']} {match['sn'][-4:]}"}
        return await self.async_step_user()

    async def async_step_bluetooth(self, discovery_info: Any) -> ConfigFlowResult:
        # an advertisement before the entry exists: the device is asked who it is over Bluetooth
        await self.async_set_unique_id(PANEL_TITLE)
        self._abort_if_unique_id_configured()
        self._add_found(await ZendureDriver.probe_ble(self.hass, str(discovery_info.address)))
        self.context["title_placeholders"] = {"name": str(discovery_info.name)}
        return await self.async_step_user()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {ZendureDriver.id: DeviceSubentryFlow, "solar_panel": SolarPanelSubentryFlow}


class DeviceSubentryFlow(ConfigSubentryFlow):
    # A manual add asks for the identity the driver needs, a reconfigure edits ip and address.
    # The name is computed once from the family and the serial, exactly like a discovered device, and never changes.

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        driver = ZendureDriver
        errors: dict = {}
        if user_input is not None:
            data = {"ip": "", "blemac": "", **user_input}
            entry = self._get_entry()
            if any(s.data.get("sn") == data["sn"] for s in entry.subentries.values()):
                errors["sn"] = "device_exists"
            elif (family := driver.device_family(data)) is None:
                errors["product"] = "unknown_product"
            else:
                title = unique_name(family, data["sn"], {s.title for s in entry.subentries.values()})
                return self.async_create_entry(title=title, data=data)
        return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(DEVICE_ADD, user_input or {}), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        sub = self._get_reconfigure_subentry()
        if user_input is not None:
            self.hass.config_entries.async_update_subentry(self._get_entry(), sub, data={**sub.data, **user_input})
            return self.async_abort(reason="reconfigure_successful")
        return self.async_show_form(step_id="reconfigure", data_schema=self.add_suggested_values_to_schema(DEVICE, {k: v for k, v in sub.data.items() if v}), description_placeholders={"name": sub.title})


class SolarPanelSubentryFlow(ConfigSubentryFlow):
    # a solar group: the entity it reports its power on, and the number entity that can throttle it when export is not allowed

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if user_input is not None:
            return self.async_create_entry(title=user_input.pop("name"), data={"power_entity": "", "derate_entity": "", **user_input})
        return self.async_show_form(step_id="user", data_schema=PANEL_ADD)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        sub = self._get_reconfigure_subentry()
        if user_input is not None:
            self.hass.config_entries.async_update_subentry(self._get_entry(), sub, data={**sub.data, **user_input})
            return self.async_abort(reason="reconfigure_successful")
        return self.async_show_form(step_id="reconfigure", data_schema=self.add_suggested_values_to_schema(PANEL, {k: v for k, v in sub.data.items() if v}), description_placeholders={"name": sub.title})
