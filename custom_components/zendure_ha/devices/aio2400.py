"""Module for the Hyper2000 device integration in Home Assistant."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.const import SmartMode
from custom_components.zendure_ha.device import ZendureDevice

_LOGGER = logging.getLogger(__name__)


class AIO2400(ZendureDevice):
    def __init__(self, hass: HomeAssistant, name: str, device_id: str, device_sn: str, model: str, model_id: str) -> None:
        """Initialise AIO2400."""
        super().__init__(hass, name, device_id, device_sn, model, model_id)
        self.setLimits(-1200, 1200)
        self.maxSolar = -1200

    async def charge(self, power: int) -> int:
        _LOGGER.info(f"Power charge {self.name} => {power}")
        self.mqttInvoke(
            {
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
            }
        )
        return power

    async def discharge(self, power: int) -> int:
        _LOGGER.info(f"Power discharge {self.name} => {power}")
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
