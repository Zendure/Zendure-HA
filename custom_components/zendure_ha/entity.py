"""Base class for Zendure entities."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityPlatformState
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.template import Template
from homeassistant.util.async_ import run_callback_threadsafe
from stringcase import snakecase

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CONST_FACTOR = 2


class EntityZendure(Entity):
    """Common elements for all Zendure entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        device: EntityDevice | None,
        uniqueid: str,
        entitytype: str,
    ) -> None:
        """Initialize a Zendure entity."""
        self._attr_has_entity_name = True
        self._attr_should_poll = False
        self._attr_available = True
        if device is None:
            return
        self.device = device
        self._attr_unique_id = f"{self.device.name}-{uniqueid}"
        self._attr_translation_key = snakecase(uniqueid)
        device.entities[uniqueid] = self

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
        "batcur": ("template", "{{ value / 10 if (value | int) < 32768 else (value | bitwise_xor(0x8000 | int) - 0x8000 | int) / 10 }}", "A", "current"),
        "BatVolt": ("template", "{{ value / 100 if (value | int) < 32768 else (value | bitwise_xor(0x8000 | int) - 0x8000 | int) / 100 }}", "V", "voltage"),
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
        "pass": ("binary"),
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
    empty = EntityZendure(None, "empty", "empty")
    to_add: dict[AddEntitiesCallback, list[EntityZendure]] = {}

    def __init__(self, hass: HomeAssistant, deviceId: str, name: str, model: str, model_id: str, parent: str | None = None) -> None:
        """Initialize Device."""
        self.hass = hass
        self.deviceId = deviceId
        self.name = name
        self.unique = "".join(self.name.split())
        self.entities: dict[str, EntityZendure] = {}

        self.attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.name)},
            name=self.name,
            manufacturer="Zendure",
            model=model,
            model_id=model_id,
            hw_version=deviceId,
        )
        device_registry = dr.async_get(self.hass)
        if di := device_registry.async_get_device(identifiers={(DOMAIN, self.name)}):
            self.attr_device_info["connections"] = di.connections
            self.attr_device_info["sw_version"] = di.sw_version

        if parent is not None:
            self.attr_device_info["via_device"] = (DOMAIN, parent)

    def add_entity(self, add: AddEntitiesCallback, entity: EntityZendure) -> None:
        toadd: list[EntityZendure] = self.to_add.get(add, [])
        toadd.append(entity)
        self.to_add[add] = toadd

    @staticmethod
    async def add_entities() -> None:
        async def doAddEntities(platforms: dict[AddEntitiesCallback, list[EntityZendure]]) -> None:
            items = list(platforms.items())
            for add, entities in items:
                add(entities)
                # Wait a short time before entities are added
                all_entities_added = False
                for _ in range(30):
                    if all_entities_added := all(ent.hasPlatform for ent in entities):
                        break
                    _LOGGER.debug("Waiting for entities to be added...")
                    await asyncio.sleep(0.1)
                if not all_entities_added:
                    _LOGGER.error("Not all entities have been added in time.")
                    break

        if EntityDevice.to_add:
            await asyncio.sleep(1)  # allow other tasks to run
            await doAddEntities(EntityDevice.to_add)
            EntityDevice.to_add = {}

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
                        entity = ZendureSensor(self, key, None, "dBm", "signal_strength", "measurement", None)
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
                        _LOGGER.debug(f"Create sensor {self.name} {key} with no unit")
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
        _LOGGER.info(f"Updating {self.name} software version from {self.attr_device_info.get('sw_version')} to {version}")
        device_registry = dr.async_get(self.hass)
        device_entry = device_registry.async_get_device(identifiers={(DOMAIN, self.name)})
        if device_entry is not None:
            run_callback_threadsafe(self.hass.loop, lambda: device_registry.async_update_device(device_entry.id, sw_version=version))
