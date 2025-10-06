"""Module for the Hyper2000 device integration in Home Assistant."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.const import SmartMode
from custom_components.zendure_ha.device import ZendureBattery, ZendureLegacy

_LOGGER = logging.getLogger(__name__)


class Hub1200(ZendureLegacy):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise Hub1200."""
        super().__init__(hass, deviceId, definition["deviceName"], prodName, definition)
        self.limitDischarge = 800
        self.limitCharge = -800
        self.maxSolar = -800

    def batteryUpdate(self, batteries: list[ZendureBattery]) -> None:
        # Check if any battery has kWh > 1
        if any(battery.kWh > 1 for battery in batteries):
            self.powerMin = -1200
            self.limitInput.update_range(0, abs(self.powerMin))

    async def power_charge(self, power: int) -> int:
        """Set charge power."""
        if abs(power - self.pwr_home) <= 1:
            _LOGGER.info(f"Power charge {self.name} => no action [power {power}]")
            return power

        _LOGGER.info(f"Power charge {self.name} => {power}")
        self.mqttInvoke({
            "arguments": [{"autoModelProgram": 2, "autoModelValue": power, "msgType": 1, "autoModel": 8}],
            "function": "deviceAutomation",
        })
        return power

    async def power_discharge(self, power: int) -> int:
        """Set discharge power."""
        if abs(power - self.pwr_home) <= 1:
            _LOGGER.info(f"Power discharge {self.name} => no action [power {power}]")
            return power

        _LOGGER.info(f"Power discharge {self.name} => {power}")
        self.mqttInvoke({
            "arguments": [{"autoModelProgram": 2, "autoModelValue": power, "msgType": 1, "autoModel": 8}],
            "function": "deviceAutomation",
        })
        return power

    async def power_off(self) -> None:
        """Set the power off."""
        self.mqttInvoke({
            "arguments": [{"autoModelProgram": 0, "autoModelValue": 0, "msgType": 1, "autoModel": 0}],
            "function": "deviceAutomation",
        })
