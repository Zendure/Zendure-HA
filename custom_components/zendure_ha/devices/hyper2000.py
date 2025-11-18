"""Module for the Hyper2000 device integration in Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.const import SmartMode
from custom_components.zendure_ha.device import ZendureLegacy
from custom_components.zendure_ha.sensor import ZendureRestoreSensor, ZendureSensor
from custom_components.zendure_ha.select import ZendureSelect

_LOGGER = logging.getLogger(__name__)


class Hyper2000(ZendureLegacy):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise Hyper2000."""
        super().__init__(hass, deviceId, definition["deviceName"], prodName, definition)
        self.dischargeLimit = 1200
        self.chargeLimit = -1200
        self.maxSolar = -1600
        self.offGrid = ZendureSensor(self, "gridOffPower", None, "W", "power", "measurement")
        self.aggrOffGrid = ZendureRestoreSensor(self, "aggrGridOffPowerTotal", None, "kWh", "energy", "total_increasing", 2)
        self.hems_State = ZendureSelect(self, "hemsState", {0: "OFF", 1: "ON"}, self.entityWrite, 1)

    @property
    def pwr_offgrid(self) -> int:
        """Get the offgrid power."""
        return self.offGrid.asInt

    async def power_charge(self, power: int) -> int:
        """Set charge power."""
        if abs(power - self.pwr_home) <= SmartMode.POWER_TOLERANCE:
            _LOGGER.info(f"Power charge {self.name} => no action [power {power}]")
            return self.pwr_home

        _LOGGER.info(f"Power charge {self.name} => {power}")
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

    async def power_discharge(self, power: int) -> int:
        """Set discharge power."""
        if abs(power - self.pwr_home) <= SmartMode.POWER_TOLERANCE:
            _LOGGER.info(f"Power discharge {self.name} => no action [power {power}]")
            return self.pwr_home

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
