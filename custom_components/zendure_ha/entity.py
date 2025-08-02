"""Base class for Zendure entities."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
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
        self._attr_device_info = device.attr_device_info
        self._attr_unique_id = f"{self._attr_device_info.get('name', None)}-{uniqueid}"
        self.entity_id = f"{entitytype}.{self._attr_device_info.get('name', None)}-{snakecase(uniqueid)}"
        self._attr_translation_key = snakecase(uniqueid)
        device.entities[uniqueid] = self

    def update_value(self, _value: Any) -> bool:
        """Update the entity value."""
        return False


class EntityDevice:
    createEntity: dict[str, Any] = {
        "power": ("W", "power"),
        "packInputPower": ("W", "power"),
        "outputPackPower": ("W", "power"),
        "outputHomePower": ("W", "power"),
        "gridInputPower": ("W", "power"),
        "gridOffPower": ("W", "power"),
        "solarInputPower": ("W", "power"),
        "solarPower1": ("W", "power"),
        "solarPower2": ("W", "power"),
        "solarPower3": ("W", "power"),
        "solarPower4": ("W", "power"),
        "solarPower5": ("W", "power"),
        "solarPower6": ("W", "power"),
        "energyPower": ("W"),
        "inverseMaxPower": ("W"),
        "BatVolt": ("V", "voltage", 100),
        "VoltWakeup": ("V", "voltage"),
        "totalVol": ("V", "voltage", 100),
        "maxVol": ("V", "voltage", 100),
        "minVol": ("V", "voltage", 100),
        "batcur": ("A", "current", 10),
        "maxTemp": ("°C", "temperature", "{{ (value | float - 2731) / 10 | round(1) }}"),
        "hyperTmp": ("°C", "temperature", "{{ (value | float - 2731) / 10 | round(1) }}"),
        "softVersion": ("version"),
        "masterSoftVersion": ("version"),
        "masterhaerVersion": ("version"),
        "dspversion": ("version"),
        "socLevel": ("%", "battery"),
        "soh": ("%", None, "{{ (value / 10) }}"),
        "electricLevel": ("%", "battery"),
        "remainOutTime": ("h", "duration"),
        "remainInputTime": ("h", "duration"),
        "masterSwitch": ("binary"),
        "buzzerSwitch": ("binary"),
        "wifiState": ("binary"),
        "heatState": ("binary"),
        "reverseState": ("binary"),
        "pass": ("binary"),
        "lowTemperature": ("binary"),
        "autoHeat": ("binary"),
        "localState": ("binary"),
        "ctOff": ("binary"),
        "lampSwitch": ("switch"),
        "invOutputPower": ("none"),
        "ambientLightNess": ("none"),
        "ambientLightColor": ("none"),
        "ambientLightMode": ("none"),
        "ambientSwitch": ("none"),
        "PowerCycle": ("none"),
        "packInputPowerCycle": ("none"),
        "outputPackPowerCycle": ("none"),
        "outputHomePowerCycle": ("none"),
    }
    empty = EntityZendure(None, "empty", "empty")

    def __init__(self, hass: HomeAssistant, deviceId: str, name: str, model: str, parent: str | None = None) -> None:
        """Initialize Device."""
        self.hass = hass
        self.deviceId = deviceId
        self.name = name
        self.unique = "".join(self.name.split())
        self.entities: dict[str, EntityZendure] = {}
        self.entitycount = 0
        self.attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.name)},
            name=self.name,
            manufacturer="Zendure",
            model=model,
            sw_version="1.0.0",
        )

        if parent is not None:
            self.attr_device_info["via_device"] = (DOMAIN, parent)

    def call_threadsafe(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        if self.hass.loop_thread_id != threading.get_ident():
            run_callback_threadsafe(self.hass.loop, func, *args, **kwargs).result()
        else:
            func(*args, **kwargs)

    async def dataRefresh(self, _update_count: int) -> None:
        return

    def entityUpdate(self, key: Any, value: Any) -> bool:
        from .binary_sensor import ZendureBinarySensor
        from .sensor import ZendureCalcSensor, ZendureSensor
        from .switch import ZendureSwitch

        # check if entity is already created
        if (entity := self.entities.get(key, None)) is None:
            if info := self.createEntity.get(key, None):
                match info if isinstance(info, str) else info[0]:
                    case "W":
                        entity = ZendureSensor(self, key, None, "W", "power", "measurement", None)
                    case "V":
                        factor = int(info[2]) if len(info) > CONST_FACTOR else 1
                        entity = ZendureSensor(self, key, None, "V", "voltage", "measurement", 1, factor)
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
                    case "version":
                        entity = ZendureCalcSensor(self, key)
                        entity.calculate = entity.calculate_version
                    case "binary":
                        entity = ZendureBinarySensor(self, key, None, "switch")
                    case "switch":
                        entity = ZendureSwitch(self, key, self.entityWrite, None, "switch", value)
                    case "none":
                        self.entities[key] = entity = self.empty
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
