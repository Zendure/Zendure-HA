"""Module for the Hyper2000 device integration in Home Assistant."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureLegacy

_LOGGER = logging.getLogger(__name__)


class AIO2400(ZendureLegacy):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise AIO2400."""
        super().__init__(hass, deviceId, definition["deviceName"], prodName, definition)
        self.limitDischarge = 1200
        self.limitCharge = -1200
        self.maxSolar = -1200

    async def power_charge(self, power: int) -> int:
        """Set charge power."""
        if abs(power - self.pwr_home) <= 1:
            _LOGGER.info(f"Power charge {self.name} => no action [power {power}]")
            return power

        _LOGGER.info(f"Power charge {self.name} => {power}")
        self.mqttInvoke({
            "arguments": [
                {
                    "autoModelProgram": 2,
                    "autoModelValue": {
                        "chargingType": 1,
                        "chargingPower": -power,
                        "freq": 0,
                        "outPower": 0,
                    },
                    "msgType": 1,
                    "autoModel": 8,
                }
            ],
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
            "arguments": [
                {
                    "autoModelProgram": 2,
                    "autoModelValue": {
                        "chargingType": 0,
                        "chargingPower": 0,
                        "freq": 0,
                        "outPower": max(0, power),
                    },
                    "msgType": 1,
                    "autoModel": 8,
                }
            ],
            "function": "deviceAutomation",
        })
        return power

    async def power_off(self) -> None:
        """Set the power off."""
        self.mqttInvoke({
            "arguments": [
                {
                    "autoModelProgram": 0,
                    "autoModelValue": {
                        "chargingType": 0,
                        "chargingPower": 0,
                        "freq": 0,
                        "outPower": 0,
                    },
                    "msgType": 1,
                    "autoModel": 0,
                }
            ],
            "function": "deviceAutomation",
        })
