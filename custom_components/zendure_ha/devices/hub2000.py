"""Module for the Hub2000 device integration in Home Assistant."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureBattery, ZendureLegacy

_LOGGER = logging.getLogger(__name__)


class Hub2000(ZendureLegacy):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise Hub2000."""
        super().__init__(hass, deviceId, definition["deviceName"], prodName, definition)
        """can get power from superbase V or satellite battery up to 1200W"""
        """power from ACE 1500 up to 900W"""
        """power to micro inverter up to 1200W"""
        self.setLimits(-1200, 1200)
        self.maxSolar = -2400

    def batteryUpdate(self, batteries: list[ZendureBattery]) -> None:
        self.powerMin = -1800 if len(batteries) > 1 else -1200 if batteries[0].kWh > 1 else -800
        self.limitInput.update_range(0, abs(self.powerMin))

    async def charge(self, power: int) -> int:
        _LOGGER.info(f"Power charge {self.name} => {power}")
        self.mqttInvoke(
            {
                "arguments": [{"autoModelProgram": 2, "autoModelValue": power, "msgType": 1, "autoModel": 8}],
                "function": "deviceAutomation",
            }
        )
        return power

    async def discharge(self, power: int) -> int:
        _LOGGER.info(f"Power discharge {self.name} => {power}")
        self.mqttInvoke(
            {
                "arguments": [{"autoModelProgram": 2, "autoModelValue": power, "msgType": 1, "autoModel": 8}],
                "function": "deviceAutomation",
            }
        )
        return power

    async def power_off(self) -> None:
        """Set the power off."""
        self.mqttInvoke(
            {
                "arguments": [{"autoModelProgram": 0, "autoModelValue": 0, "msgType": 1, "autoModel": 0}],
                "function": "deviceAutomation",
            }
        )
