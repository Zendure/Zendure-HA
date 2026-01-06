"""Module for SolarFlow800Pro integration."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureDevice
from custom_components.zendure_ha.sensor import ZendureRestoreSensor, ZendureSensor

_LOGGER = logging.getLogger(__name__)


class SolarFlow800Pro(ZendureDevice):
    def __init__(self, hass: HomeAssistant, name: str, device_id: str, device_sn: str, model: str, model_id: str) -> None:
        """Initialise SolarFlow800Pro."""
        super().__init__(hass, name, device_id, device_sn, model, model_id)
        self.setLimits(-1000, 800)
        self.maxSolar = -1200
        self.offGrid = ZendureSensor(self, "gridOffPower", None, "W", "power", "measurement")
        self.aggrOffGrid = ZendureRestoreSensor(self, "aggrGridOffPowerTotal", None, "kWh", "energy", "total_increasing", 2)

    @property
    def pwr_offgrid(self) -> int:
        """Get the offgrid power."""
        return self.offGrid.asInt
