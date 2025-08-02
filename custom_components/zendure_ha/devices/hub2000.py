"""Module for the Hyper2000 device integration in Home Assistant."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.const import ManagerState
from custom_components.zendure_ha.device import ZendureBattery, ZendureLegacy
from custom_components.zendure_ha.select import ZendureRestoreSelect

_LOGGER = logging.getLogger(__name__)


class Hub2000(ZendureLegacy):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise Hub2000."""
        super().__init__(hass, deviceId, definition["deviceName"], prodName, definition)
        self.powerMin = -800
        self.powerMax = 800
        self.passMode = ZendureRestoreSelect(self, "passMode", {0: "auto", 2: "on", 1: "off"}, self.entityWrite, 1)

    def batteryUpdate(self, batteries: list[ZendureBattery]) -> None:
        self.powerMin = -1800 if len(batteries) > 1 else -1200 if batteries[0].kWh > 1 else -800
        self.limitInput.update_range(0, abs(self.powerMin))

    def power_set(self, state: ManagerState, power: int) -> int:
        """Set the power output/input."""
        delta = abs(power - self.powerAct)
        if delta <= 2 and state != ManagerState.IDLE:
            _LOGGER.info(f"Update power {self.name} => no action [power {power}]")
            return self.powerAct

        _LOGGER.info(f"Update power {self.name} => {power} state: {state} delta: {delta}")
        self.mqttInvoke({
            "arguments": [
                {
                    "autoModelProgram": 2 if state != ManagerState.IDLE else 0,
                    "autoModelValue": power,
                    "msgType": 1,
                    "autoModel": 8 if state != ManagerState.IDLE else 0,
                }
            ],
            "function": "deviceAutomation",
        })
        return power
