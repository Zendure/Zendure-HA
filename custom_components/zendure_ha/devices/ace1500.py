"""Module for the ACE1500 device integration in Home Assistant."""

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureDevice, ZendureLegacy
from custom_components.zendure_ha.select import ZendureRestoreSelect, ZendureSelect
from custom_components.zendure_ha.switch import ZendureSwitch

_LOGGER = logging.getLogger(__name__)

# inputLimit writes go to the ACE 1500's flash memory. Quantize to 50 W
# steps and throttle to one write per 30s so a long surplus-tracking
# session doesn't burn through flash endurance.
_INPUT_LIMIT_STEP_W = 50
_INPUT_LIMIT_MIN_INTERVAL = timedelta(seconds=30)


class ACE1500(ZendureLegacy):

    _kWh: float = 0.0
    pairedHub: ZendureLegacy | None = None

    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any, parent: str | None = None) -> None:
        """Initialise Ace1500."""
        super().__init__(hass, deviceId, prodName, definition["productModel"], definition, parent)
        self.maxSolar = -900
        self.acSwitch = ZendureSwitch(self, "acSwitch", self.entityWrite, None, "switch",1)
        self.dcSwitch = ZendureSelect(self, "dcSwitch", {0: "off", 1: "on"}, self.entityWrite, 1)
        # Hub-paired vs standalone. Paired: the Hub manages the ACE, so only the
        # Hub's AC mode changes (see charge()) and the ACE counts no capacity of
        # its own. Standalone: the ACE has its own packs. Charging is the same in
        # both: direct inputLimit writes, because Smart Matching programs need a
        # Hub heartbeat the integration can't supply.
        self.hubMode = ZendureRestoreSelect(self, "hubMode", {0: "paired", 1: "standalone"}, None, 0)
        # Track the last inputLimit value/time so standalone charge writes can
        # be quantized and rate-limited to protect device flash.
        self._last_input_limit: int | None = None
        self._last_input_limit_time = datetime.min
        # The ACE 1500 cannot feed the home grid, paired or not.
        # iIs AC outlet is off-grid only, and a paired Hub puts the power out.
        # Discharge limit 0 keeps the manager from dispatching to it.
        self.setLimits(-900, 0)

    @property
    def paired(self) -> bool:
        return getattr(self, "hubMode", None) is not None and self.hubMode.value == 0

    @property
    def kWh(self) -> float:
        """Battery capacity this device contributes to the system.
        """
        return 0.0 if self.paired else self._kWh

    @kWh.setter
    def kWh(self, value: float) -> None:
        self._kWh = value

    async def charge(self, power: int) -> int:
        _LOGGER.debug("%s => Power charge %s", self.name, power)
        # Paired too: a Smart Matching (autoModel 8) command makes the Hub switch to
        # program 8 and the ACE then stops drawing from the grid.
        self._chargeStandalone(power)
        # The Hub only takes the ACE's grid power in AC mode input,
        # in output the ACE draws 0 W
        if self.pairedHub is not None and self.hubMode.value == 0:
            self.pairedHub.ac_charge = True
        return power

    async def discharge(self, power: int) -> int:
        _LOGGER.debug("%s => Power discharge", self.name)
        self._dischargeStandalone(power)
        return power

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
        write per 30 s — applies uniformly to start and stop so a surplus
        that briefly crosses the quantization step doesn't trigger a
        50W-write immediately followed by a 0W-write. The cost is that a
        sustained drop in surplus keeps the device drawing its last
        commanded value for up to one interval; that's bounded and small,
        and worth it for flash endurance."""
        target = (target // _INPUT_LIMIT_STEP_W) * _INPUT_LIMIT_STEP_W
        now = datetime.now()
        if target == self._last_input_limit:
            return
        if (
            self._last_input_limit is not None
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


def pair_aces(devices: Iterable[ZendureDevice]) -> None:
    """Give each ACE 1500 the HUB that reports its battery packs."""
    devices = list(devices)
    hubs = [d for d in devices if isinstance(d, ZendureLegacy) and not isinstance(d, ACE1500)]
    for ace in devices:
        if isinstance(ace, ACE1500):
            hub = next((h for h in hubs if h.batteries.keys() & ace.batteries.keys()), None)
            if hub is not None and hub is not ace.pairedHub:
                _LOGGER.info("%s and HUB %s have been paired", ace.name, hub.name)
            ace.pairedHub = hub
