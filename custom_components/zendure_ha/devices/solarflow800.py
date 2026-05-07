"""Module for SolarFlow800 integration."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureZenSdk
from custom_components.zendure_ha.sensor import ZendureSensor, ZendureRestoreSensor

_LOGGER = logging.getLogger(__name__)


class SolarFlow800(ZendureZenSdk):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise SolarFlow800."""
        super().__init__(hass, deviceId, prodName, definition["productModel"], definition)
        self.setLimits(-1000, 800)
        self.maxSolar = -1200


class SolarFlow800Plus(ZendureZenSdk):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise SolarFlow800Plus."""
        super().__init__(hass, deviceId, prodName, definition["productModel"], definition)
        self.setLimits(-1000, 800)
        self.maxSolar = -1500


class SolarFlow800Pro(ZendureZenSdk):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise SolarFlow800Pro."""
        super().__init__(hass, deviceId, prodName, definition["productModel"], definition)
        self.setLimits(-1000, 800)
        self.maxSolar = -1200
        self.offGrid = ZendureSensor(self, "gridOffPower", None, "W", "power", "measurement")
        self.aggrOffGrid = ZendureRestoreSensor(self, "aggrGridOffPower", None, "kWh", "energy", "total", 2)

    @property
    def pwr_offgrid(self) -> int:
        """Get the offgrid power."""
        return self.offGrid.asInt


class SolarFlow800Pro2(SolarFlow800Pro):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise SolarFlow800Pro2 (4 solar inputs vs 2 on Pro)."""
        super().__init__(hass, deviceId, prodName, definition)
        self.solarPower3 = ZendureSensor(self, "solarPower3", None, "W", "power", "measurement")
        self.solarPower4 = ZendureSensor(self, "solarPower4", None, "W", "power", "measurement")
