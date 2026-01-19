"""Module for SolarFlow800 integration."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureZenSdk
from custom_components.zendure_ha.sensor import ZendureRestoreSensor, ZendureSensor

_LOGGER = logging.getLogger(__name__)


class SolarFlow800Pro(ZendureZenSdk):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise SolarFlow800Pro."""
        super().__init__(hass, deviceId, definition["deviceName"], prodName, definition)
        self.setLimits(-1000, 800)
        self.solar_inputs = 4
        self.maxSolar = -1200
        self.offGrid = ZendureSensor(self, "gridOffPower", None, "W", "power", "measurement")
        self.aggrOffGrid = ZendureRestoreSensor(self, "aggrGridOffPowerTotal", None, "kWh", "energy", "total_increasing", 2)

    @property
    def pwr_offgrid(self) -> int:
        """Get the offgrid power."""
        return self.offGrid.asInt
