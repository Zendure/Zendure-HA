"""Module for SolarFlow800 integration."""

import logging

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureDevice

_LOGGER = logging.getLogger(__name__)


class SolarFlow800(ZendureDevice):
    def __init__(self, hass: HomeAssistant, device_id: str, device_sn: str, model: str, model_id: str) -> None:
        """Initialise SolarFlow800."""
        super().__init__(hass, device_id, device_sn, model, model_id)
        self.setLimits(-1000, 800)
        self.maxSolar = -1200
