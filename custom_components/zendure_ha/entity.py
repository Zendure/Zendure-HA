"""Base class for Zendure entities."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state as rs
from homeassistant.helpers.device_registry import DeviceEntry, DeviceInfo
from homeassistant.helpers.entity import Entity, EntityPlatformState
from homeassistant.helpers.template import Template

from .const import DOMAIN


def snakecase(value: str) -> str:
    """Convert to snake_case with only HA-valid chars (a-z, 0-9, _)."""
    # normalize unicode (e.g. ä -> a, é -> e)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    # insert underscore before uppercase letters (camelCase -> camel_case)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    # replace any non-alphanumeric character with underscore
    value = re.sub(r"[^a-z0-9]", "_", value.lower())
    # collapse multiple underscores and strip leading/trailing
    value = re.sub(r"_+", "_", value).strip("_")
    return value


_LOGGER = logging.getLogger(__name__)

CONST_FACTOR = 2


class EntityZendure(Entity):
    """Common elements for all Zendure entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        device: EntityDevice | None,
        uniqueid: str,
        domain: str = "",
    ) -> None:
        """Initialize a Zendure entity."""
        self._attr_has_entity_name = True
        self._attr_should_poll = False
        self._attr_available = True
        if device is None:
            if uniqueid != "empty":
                _LOGGER.warning("Entity %s has no device, skipping initialization.", uniqueid)
            return
        self.device = device
        self.propertyName = uniqueid
        self._attr_unique_id = snakecase(f"{self.device.name.lower()}_{uniqueid}")
        self.internal_integration_suggested_object_id = self._attr_unique_id
        self._attr_translation_key = snakecase(uniqueid)
        device.entities[uniqueid] = self
        if domain and device.checkEntity is not None and self._attr_translation_key not in device.checkEntity:
            device.checkEntity[self._attr_translation_key] = domain

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return the device info."""
        return self.device.attr_device_info

    def update_value(self, _value: Any) -> bool:
        """Update the entity value."""
        return False

    @property
    def hasPlatform(self) -> bool:
        """Return whether the entity has a platform."""
        return self._platform_state != EntityPlatformState.NOT_ADDED


class EntityDevice:
    createEntity: dict[str, Any] = {
        "power": ("W", "power"),
        "packInputPower": ("W", "power"),
        "outputPackPower": ("W", "power"),
        "outputHomePower": ("W", "power"),
        "gridInputPower": ("W", "power"),
        "gridOffPower": ("W", "power"),
        "gridPower": ("W", "power"),
        "acOutputPower": ("W", "power"),
        "dcOutputPower": ("W", "power"),
        "solarInputPower": ("W", "power", "mdi:solar-panel"),
        "solarPower1": ("W", "power"),
        "solarPower2": ("W", "power"),
        "solarPower3": ("W", "power"),
        "solarPower4": ("W", "power"),
        "solarPower5": ("W", "power"),
        "solarPower6": ("W", "power"),
        "energyPower": ("W"),
        "inverseMaxPower": ("W"),
        "batteryElectric": ("W", "power"),
        "VoltWakeup": ("V", "voltage"),
        "totalVol": ("V", "voltage", 100),
        "totalBatteryVolt": ("V", "voltage", 100),
        "maxVol": ("V", "voltage", 100),
        "minVol": ("V", "voltage", 100),
        "batcur": (
            "template",
            "{{ value / 10 if (value | int) < 32768 else (value | bitwise_xor(0x8000 | int) - 0x8000 | int) / 10 }}",
            "A",
            "current",
        ),
        "BatVolt": (
            "template",
            "{{ value / 100 if (value | int) < 32768 else (value | bitwise_xor(0x8000 | int) - 0x8000 | int) / 100 }}",
            "V",
            "voltage",
        ),
        "maxTemp": ("°C", "temperature"),
        "hyperTmp": ("°C", "temperature"),
        "softVersion": ("version"),
        "masterSoftVersion": ("version"),
        "masterhaerVersion": ("version"),
        "dspversion": ("version"),
        "mpptFirmwareVersion": ("version"),
        "dcFirmwareVersion": ("version"),
        "acFirmwareVersion": ("version"),
        "bmsFirmwareVersion": ("version"),
        "masterFirmwareVersion": ("version"),
        "dcHardwareVersion": ("version"),
        "acHardwareVersion": ("version"),
        "bmsHardwareVersion": ("version"),
        "masterHardwareVersion": ("version"),
        "socLevel": ("%", "battery"),
        "soh": ("%", None, "{{ (value / 10) }}"),
        "electricLevel": ("%", "battery"),
        "rssi": ("dBm", "signal_strength"),
        "masterSwitch": ("binary"),
        "buzzerSwitch": ("switch"),
        "autoRecover": ("switch"),
        "wifiState": ("binary"),
        "heatState": ("binary"),
        "restState": ("binary"),
        "reverseState": ("binary"),
        "lowTemperature": ("binary"),
        "autoHeat": ("select", {0: "off", 1: "on"}, 1),
        "localState": ("binary"),
        "ctOff": ("binary"),
        "lampSwitch": ("switch"),
        "gridReverse": ("select", {0: "disabled", 1: "allow", 2: "forbidden"}),
        "gridOffMode": ("select", {0: "normal", 1: "eco", 2: "off"}),
        "passMode": ("select", {0: "auto", 2: "on", 1: "off"}),
        "fanSwitch": ("switch"),
        "fanSpeed": ("select", {0: "auto", 1: "normal", 2: "fast"}),
        "Fanmode": ("switch"),
        "Fanspeed": ("select", {0: "auto", 1: "normal", 2: "fast"}),
        "invOutputPower": ("none"),
        "ambientLightNess": ("none"),
        "ambientLightColor": ("none"),
        "ambientLightMode": ("none"),
        "ambientSwitch": ("none"),
        "PowerCycle": ("none"),
        "faultLevel": ("none"),
        "oldMode": ("none"),
        "circuitCheckMode": ("none"),
        "acoutputPowerCycle": ("none"),
        "dcoutputPowerCycle": ("none"),
        "gridInputPowerCycle": ("none"),
        "packInputPowerCycle": ("none"),
        "outputPackPowerCycle": ("none"),
        "outputHomePowerCycle": ("none"),
        "solarPower1Cycle": ("none"),
        "solarPower2Cycle": ("none"),
        "ts": ("none"),
        "tsZone": ("none"),
    }
    checkEntity: dict[str, str] | None = None

    empty = EntityZendure(None, "empty")

    def __init__(
        self,
        hass: HomeAssistant,
        deviceId: str,
        name: str,
        model: str = "",
        model_id: str = "",
        sn: str = "",
        parent: str | None = None,
    ) -> None:
        """Initialize Device."""
        from .migration import Migration

        self.hass = hass
        self.deviceId = deviceId
        self.name = name or deviceId
        self.unique = "".join(self.name.split())
        self.entities: dict[str, EntityZendure] = {}
        self.sn = sn

        Migration.check_device(self.hass, deviceId, self.name, model, sn)
        self.attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, deviceId)} | {(DOMAIN, sn)},
            name=self.name,
            manufacturer="Zendure",
            model=model,
            model_id=model_id,
            serial_number=sn,
        )
        if parent is None:
            self.attr_device_info["hw_version"] = deviceId
        device_registry = dr.async_get(self.hass)
        if di := device_registry.async_get_device(identifiers={(DOMAIN, sn)}):
            self.attr_device_info["connections"] = di.connections
            self.check_entities(di, snakecase(self.name.lower()))

        if parent is not None:
            self.attr_device_info["via_device"] = (DOMAIN, parent)

    def check_entities(self, di: DeviceEntry, name: str) -> None:
        if EntityDevice.checkEntity is None:
            _t = json.loads((Path(__file__).parent / "translations" / "en.json").read_text())
            EntityDevice.checkEntity = {key: domain for domain, keys in _t.get("entity", {}).items() for key in keys}

        # Get all entities for this device and group them by translation_key if they match the current device and platform
        entity_registry = er.async_get(self.hass)
        ed: dict[str, list[er.RegistryEntry]] = {}
        for entity in er.async_entries_for_device(entity_registry, di.id, True):
            if entity.platform == DOMAIN and (dn := self.checkEntity.get(entity.translation_key)) is not None and dn == entity.domain:
                ed.setdefault(entity.translation_key, []).append(entity)

        # check al entities
        for key, entries in ed.items():
            entityid = f"{entries[0].domain}.{name}_{key}"
            if len(entries) == 1 and entries[0].entity_id == entityid:
                continue
            _LOGGER.info("Update entity %s", entityid)
            if (found := next((x for x in entries if x.entity_id == entityid), entries[0])) is not None:
                entries.remove(found)
                if found.entity_id != entityid:
                    _LOGGER.info("Updating entity %s -> %s", found.entity_id, entityid)
                    entity_registry.async_update_entity(found.entity_id, new_entity_id=entityid)

            # remove all other entities with same translation_key but different entity_id
            for entry in entries:
                _LOGGER.info("Removing entity %s", entry.entity_id)
                entity_registry.async_remove(entry.entity_id)

    async def dataRefresh(self, _update_count: int) -> None:
        return

    def entityUpdate(self, key: Any, value: Any) -> bool:  # noqa: PLR0915
        from .binary_sensor import ZendureBinarySensor
        from .select import ZendureSelect
        from .sensor import ZendureCalcSensor, ZendureSensor
        from .switch import ZendureSwitch

        # check if entity is already created
        if (entity := self.entities.get(key, None)) is None:
            if info := self.createEntity.get(key, None):
                match info if isinstance(info, str) else info[0]:
                    case "W":
                        entity = ZendureSensor(self, key, None, "W", "power", "measurement", None)
                        if len(info) >= 3:
                            entity.icon = info[2]
                    case "V":
                        factor = int(info[2]) if len(info) > CONST_FACTOR else 1
                        entity = ZendureSensor(self, key, None, "V", "voltage", "measurement", 2, factor)
                    case "%":
                        if info[1] == "battery":
                            entity = ZendureSensor(self, key, None, "%", "battery", "measurement", None)
                        else:
                            tmpl = Template(info[2], self.hass) if len(info) > CONST_FACTOR else None
                            entity = ZendureSensor(self, key, tmpl, "%", info[1], "measurement", None)
                    case "A":
                        factor = int(info[2]) if len(info) > CONST_FACTOR else 1
                        entity = ZendureSensor(self, key, None, "A", "current", "measurement", None, factor)
                    case "h":
                        tmpl = Template("{{ value | int / 60 }}", self.hass)
                        entity = ZendureSensor(self, key, tmpl, "h", "duration", "measurement", None)
                    case "°C":
                        tmpl = Template("{{ (value | float - 2731) / 10 | round(1) }}", self.hass)
                        entity = ZendureSensor(self, key, tmpl, "°C", "temperature", "measurement", None)
                    case "dBm":
                        entity = ZendureSensor(
                            self,
                            key,
                            None,
                            "dBm",
                            "signal_strength",
                            "measurement",
                            None,
                        )
                    case "version":
                        entity = ZendureCalcSensor(self, key)
                        entity.calculate = entity.calculate_version
                    case "binary":
                        entity = ZendureBinarySensor(self, key, None, "switch")
                    case "switch":
                        entity = ZendureSwitch(self, key, self.entityWrite, None, "switch", value)
                    case "none":
                        self.entities[key] = entity = self.empty
                    case "select":
                        if isinstance(info[1], dict):
                            options: Any = info[1]
                            default: Any = 0 if len(info) == 2 else info[2]
                            entity = ZendureSelect(self, key, options, self.entityWrite, default)
                    case "template":
                        tmpl = Template(info[1], self.hass)
                        entity = ZendureSensor(self, key, tmpl, info[2], info[3], "measurement", None)
                    case _:
                        _LOGGER.debug("Create sensor %s %s with no unit", self.name, key)
            else:
                entity = ZendureSensor(self, key)

            if entity is not None and entity.platform is not None:
                entity.update_value(value)
            return True

        # update entity state
        if entity is not None and entity.platform and entity.state != value:
            return entity.update_value(value)

        return False

    def entityWrite(self, _entity: EntityZendure, _value: Any) -> None:
        return

    def updateVersion(self, version: str) -> None:
        _LOGGER.info("Updating %s software version from %s to %s", self.name, self.attr_device_info.get("sw_version"), version)
        device_registry = dr.async_get(self.hass)
        identifier = self.sn or self.name
        device_entry = device_registry.async_get_device(identifiers={(DOMAIN, identifier)})
        if device_entry is not None:
            device_registry.async_update_device(device_entry.id, sw_version=version)
