"""Module for the Hyper2000 device integration in Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberMode
from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.binary_sensor import ZendureBinarySensor
from custom_components.zendure_ha.number import ZendureNumber
from custom_components.zendure_ha.select import ZendureSelect
from custom_components.zendure_ha.sensor import ZendureSensor
from custom_components.zendure_ha.switch import ZendureSwitch
from custom_components.zendure_ha.zenduredevice import ZendureDevice

_LOGGER = logging.getLogger(__name__)


class Hyper2000(ZendureDevice):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise Hyper2000."""
        super().__init__(hass, deviceId, prodName, definition)
        self.powerMin = -1200
        self.powerMax = 800
        self.numbers: list[ZendureNumber] = []

    def entitiesCreate(self) -> None:
        super().entitiesCreate()

        binaries = [
            self.binary("masterSwitch"),
            self.binary("buzzerSwitch"),
            self.binary("wifiState"),
            self.binary("heatState"),
            self.binary("reverseState"),
            self.binary("pass"),
            self.binary("lowTemperature"),
            self.binary("autoHeat"),
            self.binary("ambientSwitch"),
            self.binary("localState"),
            self.binary("ctOff"),
        ]
        ZendureBinarySensor.add(binaries)

        self.numbers = [
            self.number("inputLimit", None, "W", "power", 0, 1200, NumberMode.SLIDER),
            self.number("outputLimit", None, "W", "power", 0, 200, NumberMode.SLIDER),
            self.number("socSet", "{{ value | int / 10 }}", "%", None, 5, 100, NumberMode.SLIDER),
            self.number("minSoc", "{{ value | int / 10 }}", "%", None, 5, 100, NumberMode.SLIDER),
        ]
        ZendureNumber.add(self.numbers)

        switches = [
            self.switch("lampSwitch"),
        ]
        ZendureSwitch.add(switches)

        sensors = [
            # self.sensor("chargingMode"),
            self.sensor("autoModel"),
            self.sensor("hubState"),
            self.sensor("solarInputPower", None, "W", "power", "measurement"),
            self.sensor("BatVolt", None, "V", "voltage", "measurement"),
            self.sensor("packInputPower", None, "W", "power", "measurement"),
            self.sensor("outputPackPower", None, "W", "power", "measurement"),
            self.sensor("outputHomePower", None, "W", "power", "measurement"),
            self.sensor("packNum", None),
            self.sensor("electricLevel", None, "%", "battery", "measurement"),
            self.sensor("energyPower", None, "W"),
            self.sensor("inverseMaxPower", None, "W"),
            self.sensor("solarPower1", None, "W", "power", "measurement"),
            self.sensor("solarPower2", None, "W", "power", "measurement"),
            self.sensor("gridInputPower", None, "W", "power", "measurement"),
            self.sensor("socStatus", None),
            self.sensor("strength", None),
            self.sensor("hyperTmp", "{{ (value | float - 2731) / 10 | round(1) }}", "°C", "temperature", "measurement"),
            self.sensor("packState"),
            self.version("masterSoftVersion"),
            self.version("masterhaerVersion"),
            self.sensor("inputMode"),
            self.sensor("blueOta"),
            self.sensor("plugState"),
            self.sensor("pvBrand"),
            self.sensor("VoltWakeup", None, "V", "voltage", "measurement"),
            self.sensor("ambientLightNess"),
            self.sensor("ambientLightColor"),
            self.sensor("ambientLightMode"),
            self.sensor("OldMode"),
            self.sensor("circuitCheckMode"),
            self.version("dspversion"),
            self.sensor("gridOffMode"),
        ]
        ZendureSensor.add(sensors)

        self.nosensor(["remainOutTime"])
        self.nosensor(["remainInputTime"])
        self.nosensor(["invOutputPower"])

        selects = [
            self.select("acMode", {1: "input", 2: "output"}, self.update_ac_mode),
            self.select("gridReverse", {0: "auto", 1: "on", 2: "off"}),
        ]
        ZendureSelect.add(selects)

    def entityUpdate(self, key: Any, value: Any) -> bool:
        # Call the base class entityUpdate method
        if not super().entityUpdate(key, value):
            return False
        match key:
            case "inverseMaxPower":
                self.powerMax = value
                self.numbers[1].update_range(0, value)
        return True

    def writePower(self, power: int, inprogram: bool) -> None:
        delta = abs(power - self.powerAct)
        if delta <= 1 and inprogram:
            _LOGGER.info(f"Update power {self.name} => no action [power {power} capacity {self.capacity}]")
            return

        _LOGGER.info(f"Update power {self.name} => {power} capacity {self.capacity} program: {inprogram}")
        self.mqttInvoke({
            "arguments": [
                {
                    "autoModelProgram": 2 if inprogram else 0,
                    "autoModelValue": {
                        "chargingType": 0 if power >= 0 else 1,
                        "chargingPower": 0 if power >= 0 else -power,
                        "freq": 2 if delta < 100 else 1 if delta < 200 else 0,
                        "outPower": max(0, power),
                    },
                    "msgType": 1,
                    "autoModel": 8 if inprogram else 0,
                }
            ],
            "function": "deviceAutomation",
        })
