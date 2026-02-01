"""Module for the Solarflow800 devices integration in Home Assistant."""

import logging

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureDevice
from custom_components.zendure_ha.sensor import ZendureRestoreSensor, ZendureSensor

_LOGGER = logging.getLogger(__name__)


class SolarFlow800(ZendureDevice):
    def __init__(self, hass: HomeAssistant, device_id: str, device_sn: str, model: str, model_id: str) -> None:
        """Initialise SolarFlow800."""
        super().__init__(hass, device_id, device_sn, model, model_id, None, True, 2)
        self.setLimits(-1000, 800)
        self.maxSolar = -1200


class SolarFlow800Plus(ZendureDevice):
    def __init__(self, hass: HomeAssistant, device_id: str, device_sn: str, model: str, model_id: str) -> None:
        """Initialise SolarFlow800Plus."""
        super().__init__(hass, device_id, device_sn, model, model_id, None, True, 0)
        self.setLimits(-1000, 800)
        self.maxSolar = -1500


class SolarFlow800Pro(ZendureDevice):
    def __init__(self, hass: HomeAssistant, device_id: str, device_sn: str, model: str, model_id: str) -> None:
        """Initialise SolarFlow800Pro."""
        super().__init__(hass, device_id, device_sn, model, model_id, None, True, 4)
        self.setLimits(-1000, 800)
        self.maxSolar = -1200
        self.offGrid = ZendureSensor(self, "gridOffPower", None, "W", "power", "measurement")
        self.aggrOffGrid = ZendureRestoreSensor(self, "aggrOffGridTotal", None, "kWh", "energy", "total_increasing", 2)
