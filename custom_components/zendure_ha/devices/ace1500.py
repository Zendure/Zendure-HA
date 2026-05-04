"""Module for the ACE1500 device integration in Home Assistant."""

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureLegacy
from custom_components.zendure_ha.select import ZendureRestoreSelect, ZendureSelect
from custom_components.zendure_ha.switch import ZendureSwitch

_LOGGER = logging.getLogger(__name__)

# inputLimit writes go to the ACE 1500's flash memory. Quantize to 50 W
# steps and throttle to one write per 30s so a long surplus-tracking
# session doesn't burn through flash endurance.
_INPUT_LIMIT_STEP_W = 50
_INPUT_LIMIT_MIN_INTERVAL = timedelta(seconds=30)


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
        # Track the last inputLimit value/time so standalone charge writes can
        # be quantized and rate-limited to protect device flash.
        self._last_input_limit: int | None = None
        self._last_input_limit_time = datetime.min

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
        """Standalone charge: park the device in autoModel=0 (None program)
        and drive inputLimit through a properties/write. Smart Matching /
        Battery Priority / Smart CT modes all need a Hub heartbeat we can't
        supply, so we route around them via direct property writes."""
        self._writeInputLimit(-power)

    def _dischargeStandalone(self, _power: int) -> None:
        """Standalone 'discharge' is effectively just stop-charging. The ACE
        1500 standalone has no AC home output (only off-grid socket / USB /
        XT-60, none of which the integration can power-control), so the
        outputLimit property has no effect. We just clear inputLimit to
        ensure the device isn't pulling from grid."""
        self._writeInputLimit(0)

    def _writeInputLimit(self, target: int) -> None:
        """Write inputLimit via properties/write, with quantization
        and rate-limiting. Each property write goes to the ACE 1500's flash
        memory, so an unguarded 5-second control loop would burn through
        endurance in months. Quantize to 50 W steps and throttle to one
        write per 30 s. target=0 (stop charging) is exempted from the rate
        limit so we can drop charging promptly when surplus disappears."""
        target = (target // _INPUT_LIMIT_STEP_W) * _INPUT_LIMIT_STEP_W
        now = datetime.now()
        if target == self._last_input_limit:
            return
        if (
            target != 0
            and self._last_input_limit is not None
            and now - self._last_input_limit_time < _INPUT_LIMIT_MIN_INTERVAL
        ):
            return
        self._setAutoModelNone()
        self._messageid += 1
        self.mqttPublish(
            self.topic_write,
            {"properties": {"inputLimit": target}},
        )
        self._last_input_limit = target
        self._last_input_limit_time = now

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
