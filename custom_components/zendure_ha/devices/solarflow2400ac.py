"""Module for the Solarflow2400AC device integration in Home Assistant."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureZenSdk
from custom_components.zendure_ha.select import ZendureRestoreSelect

_LOGGER = logging.getLogger(__name__)


class SolarFlow2400AC(ZendureZenSdk):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise SolarFlow2400AC."""
        super().__init__(hass, deviceId, definition["deviceName"], prodName, definition)
        self.powerMin = -2400
        self.powerMax = 2400
        self.gridReverse = ZendureRestoreSelect(self, "gridReverse", {0: "auto", 1: "on", 2: "off"}, self.entityWrite, 1)
