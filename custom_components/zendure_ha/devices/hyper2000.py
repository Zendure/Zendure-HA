"""Module for the Hyper2000 device integration in Home Assistant."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.number import NumberMode
from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.binary_sensor import ZendureBinarySensor
from custom_components.zendure_ha.number import ZendureNumber
from custom_components.zendure_ha.select import ZendureSelect
from custom_components.zendure_ha.sensor import ZendureSensor
from custom_components.zendure_ha.switch import ZendureSwitch
from custom_components.zendure_ha.zenduredevice import ZendureDevice, ZendureDeviceDefinition

_LOGGER = logging.getLogger(__name__)


class Hyper2000(ZendureDevice):
    def __init__(self, hass: HomeAssistant, h_id: str, definition: ZendureDeviceDefinition) -> None:
        """Initialise Hyper2000."""
        super().__init__(hass, h_id, definition, "Hyper 2000")
        self.powerMin = -1200
        self.powerMax = 800
        self.numbers: list[ZendureNumber] = []

    def sensorsCreate(self) -> None:
        super().sensorsCreate()

        binairies = [
            self.binary("masterSwitch", None, "switch"),
            self.binary("buzzerSwitch", None, "switch"),
            self.binary("wifiState", None, "switch"),
            self.binary("heatState", None, "switch"),
            self.binary("reverseState", None, "switch"),
            self.binary("lowTemperature", None, "switch"),
        ]
        ZendureBinarySensor.addBinarySensors(binairies)

        self.numbers = [
            self.number("inputLimit", None, "W", "power", 0, 1200, NumberMode.SLIDER),
            self.number("outputLimit", None, "W", "power", 0, 200, NumberMode.SLIDER),
            self.number("socSet", "{{ value | int / 10 }}", "%", None, 5, 100, NumberMode.SLIDER),
            self.number("minSoc", "{{ value | int / 10 }}", "%", None, 5, 100, NumberMode.SLIDER),
        ]
        ZendureNumber.addNumbers(self.numbers)

        switches = [
            self.switch("lampSwitch", None, "switch"),
        ]
        ZendureSwitch.addSwitches(switches)

        sensors = [
            # self.sensor("chargingMode"),
            self.sensor("hubState"),
            self.sensor("solarInputPower", None, "W", "power", "measurement"),
            self.sensor("batVolt", None, "V", "voltage", "measurement"),
            self.sensor("packInputPower", None, "W", "power", "measurement"),
            self.sensor("outputPackPower", None, "W", "power", "measurement"),
            self.sensor("outputHomePower", None, "W", "power", "measurement"),
            self.sensor("remainOutTime", "{{ (value / 60) }}", "h", "duration"),
            self.sensor("remainInputTime", "{{ (value / 60) }}", "h", "duration"),
            self.sensor("packNum", None),
            self.sensor("electricLevel", None, "%", "battery", "measurement"),
            self.sensor("energyPower", None, "W"),
            self.sensor("inverseMaxPower", None, "W"),
            self.sensor("solarPower1", None, "W", "power", "measurement"),
            self.sensor("solarPower2", None, "W", "power", "measurement"),
            self.sensor("gridInputPower", None, "W", "power", "measurement"),
            self.sensor("packInputPowerCycle", None, "W", "power", "measurement"),
            self.sensor("outputHomePowerCycle", None, "W", "power", "measurement"),
            self.sensor("pass", None),
            self.sensor("socStatus", None),
            self.sensor("strength", None),
            self.sensor("hyperTmp", "{{ (value | float/10 - 273.15) | round(2) }}", "°C", "temperature", "measurement"),
        ]
        ZendureSensor.addSensors(sensors)

        selects = [
            self.select(
                "acMode",
                {1: "input", 2: "output"},
                self.update_ac_mode,
            )
        ]

        ZendureSelect.addSelects(selects)

    def updateProperty(self, key: Any, value: Any) -> bool:
        # Call the base class updateProperty method
        if not super().updateProperty(key, value):
            return False
        match key:
            case "inverseMaxPower":
                self.powerMax = value
                self.numbers[1].update_range(0, value)
        return True

    def powerSet(self, power: int, inprogram: bool) -> None:
        delta = abs(power - self.powerAct)
        if delta == 0:
            _LOGGER.info(f"Update power {self.name} => no action [power {power} capacity {self.capacity}]")
            return

        _LOGGER.info(f"Update power {self.name} => {power} capacity {self.capacity}")
        self.function_invoke({
            "arguments": [
                {
                    "autoModelProgram": 2 if inprogram else 0,
                    "autoModelValue": {
                        "chargingType": 0 if power > 0 else 1,
                        "chargingPower": 0 if power > 0 else -power,
                        "freq": 2 if delta < 100 else 1 if delta < 200 else 0,
                        "outPower": max(0, power),
                    },
                    "msgType": 1,
                    "autoModel": 8 if power != 0 else 0,
                }
            ],
            "deviceKey": self.hid,
            "function": "deviceAutomation",
            "messageId": self._messageid,
            "timestamp": int(datetime.now().timestamp()),
        })
