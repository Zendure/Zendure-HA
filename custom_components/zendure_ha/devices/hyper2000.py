"""Module for the Hyper2000 device integration in Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureDevice

_LOGGER = logging.getLogger(__name__)


class Hyper2000(ZendureDevice):
    def __init__(self, hass: HomeAssistant, device_id: str, device_sn: str, model: str, model_id: str) -> None:
        """Initialise Hyper2000."""
        super().__init__(hass, device_id, device_sn, model, model_id)
        self.setLimits(-1200, 1200)
        self.maxSolar = -1600

    def power_update(self, power: int) -> int:
        if power < 0:
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
        else:
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
