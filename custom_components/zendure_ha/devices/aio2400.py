"""Module for the AIO 2400 device integration in Home Assistant."""

import logging
from typing import Any

from homeassistant.components.number import NumberMode
from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.const import SmartMode
from custom_components.zendure_ha.device import ZendureLegacy
from custom_components.zendure_ha.number import ZendureRestoreNumber

_LOGGER = logging.getLogger(__name__)


class AIO2400(ZendureLegacy):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise AIO2400."""
        super().__init__(hass, deviceId, prodName, definition["productModel"], definition)
        """AIO 2400 cannot charge using AC"""
        self.setLimits(0, 1200)
        self.maxSolar = -1200
        self.minOutputPower = ZendureRestoreNumber(self, "min_output_power", self.localEntityWrite, None, "W", "power", 800, 0, NumberMode.SLIDER)

    async def charge(self, power: int) -> int:
        _LOGGER.info("No AC charge for %s available", self.name)
        return 0

    async def discharge(self, power: int) -> int:
        _LOGGER.info("Power discharge %s => %s", self.name, power)
        self.mqttInvoke(
            {
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
