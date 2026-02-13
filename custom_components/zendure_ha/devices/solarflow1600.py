"""Module for the Solarflow2400 devices integration in Home Assistant."""

import logging

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureDevice
from custom_components.zendure_ha.sensor import ZendureRestoreSensor, ZendureSensor

_LOGGER = logging.getLogger(__name__)


class SolarFlow1600AC(ZendureDevice):
    def __init__(self, hass: HomeAssistant, device_id: str, device_sn: str, model: str, model_id: str) -> None:
        """Initialise SolarFlow1600AC."""
        super().__init__(hass, device_id, device_sn, model, model_id, None, True, 0)
        self.setLimits(-1600, 1600)
        self.maxSolar = -1600
        self.offGrid = ZendureSensor(self, "gridOffPower", None, "W", "power", "measurement")
        self.aggrOffGrid = ZendureRestoreSensor(self, "aggrOffGridTotal", None, "kWh", "energy", "total_increasing", 2)
