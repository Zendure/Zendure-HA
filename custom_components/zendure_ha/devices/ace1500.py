"""Module for the ACE1500 device integration in Home Assistant."""

import logging

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureDevice

_LOGGER = logging.getLogger(__name__)


class ACE1500(ZendureDevice):
    def __init__(self, hass: HomeAssistant, device_id: str, device_sn: str, model: str, model_id: str, parent: str | None = None) -> None:
        """Initialise Ace1500."""
        super().__init__(hass, device_id, device_sn, model, model_id, parent, False, 2)
        self.setLimits(-900, 800)
        self.maxSolar = -900

    def power_update(self, power: int) -> int:
        if power < 0:
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
                                "outPower": max(0, power + sp),
                            },
                            "msgType": 1,
                            "autoModel": 8,
                        }
                    ],
                    "function": "deviceAutomation",
                }
            )
        return power

    def power_off(self) -> None:
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
