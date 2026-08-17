"""Module for the Hyper2000 device integration in Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureLegacy
from custom_components.zendure_ha.sensor import ZendureRestoreSensor, ZendureSensor

_LOGGER = logging.getLogger(__name__)


class Hyper2000(ZendureLegacy):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise Hyper2000."""
        super().__init__(hass, deviceId, prodName, definition["productModel"], definition)
        self.setLimits(-1200, 1200)
        self.maxSolar = -1600

    async def charge(self, power: int) -> int:
        _LOGGER.info("Power charge %s => %s", self.name, power)
        self.mqttInvoke(
            {
                "arguments": [
                    {
                        "autoModelProgram": 1,
                        "autoModelValue": {
                            "chargingType": 1,
                            "price": 2,
                            "chargingPower": -power,
                            "prices": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
                            "outPower": 0,
                            "freq": 0,
                        },
                        "msgType": 1,
                        "autoModel": 8,
                    }
                ],
                "function": "deviceAutomation",
            }
        )
        return power

    async def discharge(self, power: int) -> int:
        _LOGGER.info("Power discharge %s => %sW (SoC %s%%)", self.name, power, self.electricLevel.asInt)
        self.mqttInvoke(
            {
                "arguments": [
                    {
                        "autoModelProgram": 2,
                        "autoModelValue": {
                            "chargingType": 0,
                            "chargingPower": 0,
                            "freq": 0,
                            "outPower": power,
                        },
                        "msgType": 1,
                        "autoModel": 8,
                    }
                ],
                "function": "deviceAutomation",
            }
        )
        return power

    async def power_off(self) -> None:
        """Set the power off."""
        self.mqttInvoke(
            {
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
            }
        )
