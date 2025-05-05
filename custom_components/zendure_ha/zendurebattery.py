"""Zendure Integration device."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .sensor import ZendureSensor
from .zendurebase import ZendureBase

_LOGGER = logging.getLogger(__name__)


class ZendureBattery(ZendureBase):
    """A Zendure Battery."""

    batterydict: dict[str, ZendureBattery] = {}

    def __init__(self, hass: HomeAssistant, name: str, model: str, snNumber: str, parent: str, kwh: int) -> None:
        """Initialize ZendureBattery."""
        super().__init__(hass, name, model, snNumber, parent)
        self.batterydict[snNumber] = self
        self.kwh = kwh

    def entitiesCreate(self, parent: ZendureBase) -> None:
        sensors = [
            self.sensor("totalVol", "{{ (value / 100) }}", "V", "voltage", "measurement"),
            self.sensor("maxVol", "{{ (value / 100) }}", "V", "voltage", "measurement"),
            self.sensor("minVol", "{{ (value / 100) }}", "V", "voltage", "measurement"),
            self.sensor("batcur", "{{ (value / 10) }}", "A", "current", "measurement"),
            self.sensor("state"),
            self.sensor("power", None, "W", "power", "measurement"),
            self.sensor("socLevel", None, "%", "battery", "measurement"),
            self.sensor("maxTemp", "{{ (value | float/10 - 273.15) | round(2) }}", "°C", "temperature", "measurement"),
            self.sensor("softVersion"),
        ]
        if parent.name == "Hub 1200" or parent.name == "Hub 2000":
            sensors.append(self.sensor("soh", "{{ (value / 10) }}", "%", None))

        ZendureSensor.addSensors(sensors)
