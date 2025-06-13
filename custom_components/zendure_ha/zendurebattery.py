"""Zendure Integration device."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant

from .sensor import ZendureSensor
from .zendurebase import ZendureBase

_LOGGER = logging.getLogger(__name__)


class ZendureBattery(ZendureBase):
    """A Zendure Battery."""

    batterydict: dict[str, ZendureBattery] = {}
    batterycontrol: dict[str, int] = {}

    def __init__(self, hass: HomeAssistant, name: str, model: str, snNumber: str, parent: str, kwh: float) -> None:
        if (self.batterycontrol.get(snNumber, None) is None):
            self.batterycontrol[snNumber] = 0
            _LOGGER.info(f"First occurrence of battery: {snNumber}")
        else:
            self.batterycontrol[snNumber] = self.batterycontrol[snNumber]+1
            
        if self.batterycontrol[snNumber]>2:
            """Initialize ZendureBattery."""
            _LOGGER.info(f"Add battery with SN: {snNumber} to the devices")
            super().__init__(hass, name, model, snNumber, parent)
            self.batterydict[snNumber] = self
            self.kwh = kwh

    def entitiesCreate(self, addsensors: Callable[[ZendureBattery, list[ZendureSensor]], None], event: Any) -> None:
        sensors = [
            self.sensor("totalVol", "{{ (value / 100) }}", "V", "voltage", "measurement"),
            self.sensor("maxVol", "{{ (value / 100) }}", "V", "voltage", "measurement"),
            self.sensor("minVol", "{{ (value / 100) }}", "V", "voltage", "measurement"),
            self.sensor("batcur", "{{ (value / 10) }}", "A", "current", "measurement"),
            self.sensor("state"),
            self.sensor("power", None, "W", "power", "measurement"),
            self.sensor("socLevel", None, "%", "battery", "measurement"),
            self.sensor("maxTemp", "{{ (value | float - 2731) / 10 | round(1) }}", "°C", "temperature", "measurement"),
            self.version("softVersion"),
        ]

        addsensors(self, sensors)
        ZendureSensor.add(sensors)
        event.set()
