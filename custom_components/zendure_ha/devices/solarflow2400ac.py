"""Module for the Hyper2000 device integration in Home Assistant."""

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


class SolarFlow2400AC(ZendureDevice):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise SolarFlow2400AC."""
        super().__init__(hass, deviceId, prodName, definition)
        self.powerMin = -2400
        self.powerMax = 2400
        self.numbers: list[ZendureNumber] = []

    def entitiesCreate(self) -> None:
        super().entitiesCreate()

        binaries = [
            self.binary("masterSwitch"),
            self.binary("buzzerSwitch"),
            self.binary("wifiState"),
            self.binary("heatState"),
            self.binary("reverseState"),
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
            self.sensor("hubState"),
            self.sensor("solarInputPower", None, "W", "power", "measurement"),
            self.sensor("BatVolt", None, "V", "voltage", "measurement"),
            self.sensor("packInputPower", None, "W", "power", "measurement"),
            self.sensor("outputPackPower", None, "W", "power", "measurement"),
            self.sensor("outputHomePower", None, "W", "power", "measurement"),
            self.sensor("packNum", None),
            self.sensor("electricLevel", None, "%", "battery"),
            self.sensor("energyPower", None, "W"),
            self.sensor("inverseMaxPower", None, "W"),
            self.sensor("solarPower1", None, "W", "power", "measurement"),
            self.sensor("solarPower2", None, "W", "power", "measurement"),
            self.sensor("gridInputPower", None, "W", "power", "measurement"),
            self.sensor("pass", None),
            self.sensor("strength", None),
        ]
        ZendureSensor.add(sensors)

        self.nosensor(["remainOutTime"])
        self.nosensor(["remainInputTime"])

        selects = [self.select("acMode", {1: "input", 2: "output"}, self.update_ac_mode)]
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

        _LOGGER.info(f"Update power {self.name} => {power} capacity {self.capacity}")
        self.mqttInvoke({
            "function": "hemsEP",
            "arguments": {
                "outputPower": max(0, power),
                "chargeState": 0 if power >= 0 else 1,
                "chargePower": 0 if power >= 0 else -power,
                "mode": 9 if inprogram else 0,
            },
        })
