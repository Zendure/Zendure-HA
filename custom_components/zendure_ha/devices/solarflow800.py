"""Module for SolarFlow800 integration."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureZenSdk
from custom_components.zendure_ha.select import ZendureRestoreSelect

_LOGGER = logging.getLogger(__name__)


class SolarFlow800(ZendureZenSdk):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise SolarFlow800."""
        super().__init__(hass, deviceId, definition["deviceName"], prodName, definition)
        self.powerMin = -1200
        self.powerMax = 800
        self.gridReverse = ZendureRestoreSelect(self, "gridReverse", {0: "auto", 1: "on", 2: "off"}, self.entityWrite, 1)
