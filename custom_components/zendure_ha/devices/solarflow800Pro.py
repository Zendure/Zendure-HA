"""Module for SolarFlow800 integration."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureZenSdk
from custom_components.zendure_ha.sensor import ZendureSensor

_LOGGER = logging.getLogger(__name__)


class SolarFlow800Pro(ZendureZenSdk):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise SolarFlow800Pro."""
        super().__init__(hass, deviceId, definition["deviceName"], prodName, definition)
        self.powerMin = -2400
        self.powerMax = 2400
        self.gridOffPower = ZendureSensor(self, "gridOffPower", None, "W", "power", "measurement")

