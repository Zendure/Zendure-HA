"""Module for the Hyper2000 device integration in Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.const import ManagerState
from custom_components.zendure_ha.device import ZendureLegacy
from custom_components.zendure_ha.select import ZendureRestoreSelect

_LOGGER = logging.getLogger(__name__)


class Hyper2000(ZendureLegacy):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise Hyper2000."""
        super().__init__(hass, deviceId, definition["deviceName"], prodName, definition)
        self.powerMin = -1200
        self.powerMax = 800
        self.gridReverse = ZendureRestoreSelect(self, "gridReverse", {0: "auto", 1: "on", 2: "off"}, self.entityWrite, 1)

    def power_set(self, state: ManagerState, power: int) -> int:
        """Set the power output/input."""
        delta = abs(power - self.powerAct)
        if delta <= 2 and state != ManagerState.IDLE:
            _LOGGER.info(f"Update power {self.name} => no action [power {power}]")
            return self.powerAct

        _LOGGER.info(f"Update power {self.name} => {power} state: {state} delta: {delta}")
        if state == ManagerState.CHARGING:
            self.mqttInvoke({
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
            })
        else:
            self.mqttInvoke({
                "arguments": [
                    {
                        "autoModelProgram": 2 if state == ManagerState.DISCHARGING else 0,
                        "autoModelValue": {
                            "chargingType": 0,
                            "chargingPower": 0,
                            "freq": 0,
                            "outPower": power,
                        },
                        "msgType": 1,
                        "autoModel": 8 if state == ManagerState.DISCHARGING else 0,
                    }
                ],
                "function": "deviceAutomation",
            })

        return power
