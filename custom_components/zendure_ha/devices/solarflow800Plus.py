"""Module for SolarFlow800 Plus integration."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureZenSdk

_LOGGER = logging.getLogger(__name__)


class SolarFlow800Plus(ZendureZenSdk):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise SolarFlow800Plus."""
        super().__init__(hass, deviceId, definition["deviceName"], prodName, definition)
        self.setLimits(-1000, 800)
        self.solar_inputs = 2
        self.maxSolar = -1500
