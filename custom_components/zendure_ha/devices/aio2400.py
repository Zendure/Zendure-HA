"""Module for the Hyper2000 device integration in Home Assistant."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.const import ManagerState, SmartMode
from custom_components.zendure_ha.device import ZendureLegacy

_LOGGER = logging.getLogger(__name__)


class AIO2400(ZendureLegacy):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise AIO2400."""
        super().__init__(hass, deviceId, definition["deviceName"], prodName, definition)
        self.power_limits(-1200, 1200)
        self.maxSolar = -1200

    def power_charge(self, power: int) -> int:
        """Set charge power."""
        curPower = self.packInputPower.asInt - self.gridInputPower.asInt
        delta = abs(power - curPower)
        if delta <= SmartMode.IGNORE_DELTA:
            _LOGGER.info(f"Power charge {self.name} => no action [power {curPower}]")
            return curPower

        power = min(0, max(self.maxCharge, power))
        if (solar := self.solarInputPower.asInt) > 0:
            power = max(power, self.maxSolar + solar)
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

    def power_discharge(self, power: int) -> int:
        """Set discharge power."""
        curPower = self.packInputPower.asInt - self.gridInputPower.asInt
        delta = abs(power - curPower)
        if delta <= SmartMode.IGNORE_DELTA:
            # _LOGGER.info(f"Power discharge {self.name} => no action [power {curPower}]")
            return curPower

        _LOGGER.info(f"Power discharge {self.name} => power {curPower}")
        power = max(0, min(self.maxDischarge, power))
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

    def power_off(self) -> None:
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
