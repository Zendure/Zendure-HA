"""Module for the ACE1500 device integration in Home Assistant."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureLegacy
from custom_components.zendure_ha.select import ZendureRestoreSelect, ZendureSelect
from custom_components.zendure_ha.switch import ZendureSwitch

_LOGGER = logging.getLogger(__name__)


class ACE1500(ZendureLegacy):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any, parent: str | None = None) -> None:
        """Initialise Ace1500."""
        super().__init__(hass, deviceId, prodName, definition["productModel"], definition, parent)
        self.setLimits(-900, 800)
        self.maxSolar = -900
        self.acSwitch = ZendureSwitch(self, "acSwitch", self.entityWrite, None, "switch",1)
        self.dcSwitch = ZendureSelect(self, "dcSwitch", {0: "off", 1: "on"}, self.entityWrite, 1)
        # Hub-paired vs standalone control mode. The ACE 1500 firmware accepts
        # different command shapes depending on whether a Hub is driving it:
        #   - paired: Smart Matching (autoModel=8) with chargingPower in
        #     autoModelValue, the historical integration default.
        #   - standalone: autoModel=0 (None program) with direct
        #     inputLimit/outputLimit property writes. Smart Matching/Battery
        #     Priority/Smart CT modes all need a Hub heartbeat the integration
        #     can't supply, so they sit in standby when no Hub is present.
        # Default is "paired" to preserve existing behavior; users without a
        # Hub need to flip this to "standalone" for charge/discharge to work.
        self.hubMode = ZendureRestoreSelect(self, "hubMode", {0: "paired", 1: "standalone"}, None, 0)

    async def charge(self, power: int) -> int:
        _LOGGER.info("Power charge %s => %s", self.name, power)
        if self.hubMode.value == 1:
            self._chargeStandalone(power)
        else:
            self._chargeViaHub(power)
        return power

    async def discharge(self, power: int) -> int:
        _LOGGER.info("Power discharge %s => %s", self.name, power)
        if self.hubMode.value == 1:
            self._dischargeStandalone(power)
        else:
            self._dischargeViaHub(power)
        return power

    def _chargeViaHub(self, power: int) -> None:
        """Hub-paired path: Smart Matching mode (autoModel=8) with the charge
        target carried in autoModelValue.chargingPower. The Hub's periodic
        commands keep the device acting on the value."""
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

    def _dischargeViaHub(self, power: int) -> None:
        """Hub-paired path: Smart Matching with discharge target carried in
        autoModelValue.outPower."""
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

    def _chargeStandalone(self, power: int) -> None:
        """Standalone path: park the device in autoModel=0 (None program) and
        drive inputLimit through a properties/write. The Smart Matching /
        Battery Priority / Smart CT modes all need a Hub heartbeat we can't
        supply, leaving the device in standby — so we route around them."""
        self._setAutoModelNone()
        self._messageid += 1
        self.mqttPublish(
            self.topic_write,
            {"properties": {"acMode": 1, "inputLimit": -power}},
        )

    def _dischargeStandalone(self, power: int) -> None:
        """Standalone path: same idea as _chargeStandalone but driving
        outputLimit, with inputLimit cleared so the firmware doesn't try to
        charge simultaneously."""
        self._setAutoModelNone()
        self._messageid += 1
        self.mqttPublish(
            self.topic_write,
            {"properties": {"acMode": 2, "outputLimit": max(0, power), "inputLimit": 0}},
        )

    def _setAutoModelNone(self) -> None:
        """Park the device in autoModel=0 (None program). Standalone ACE 1500
        only supports autoModel values 0/7/10; the others (6, 8, 9) require a
        paired Hub. None is the simplest fit for direct external control."""
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
