"""Module for the Hyper2000 device integration in Home Assistant."""

import logging

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.battery import ZendureBattery
from custom_components.zendure_ha.device import ZendureDevice

_LOGGER = logging.getLogger(__name__)


class Hub1200(ZendureDevice):
    def __init__(self, hass: HomeAssistant, name: str, device_id: str, device_sn: str, model: str, model_id: str) -> None:
        """Initialise Hub1200."""
        super().__init__(hass, name, device_id, device_sn, model, model_id)
        self.setLimits(-800, 800)
        self.maxSolar = -800

    def batteryUpdate(self, batteries: list[ZendureBattery]) -> None:
        # Check if any battery has kWh > 1
        if any(battery.kWh > 1 for battery in batteries):
            self.powerMin = -1200
            self.limitInput.update_range(0, abs(self.powerMin))

    async def charge(self, power: int) -> int:
        _LOGGER.info(f"Power charge {self.name} => {power}")
        self.mqttInvoke(
            {
                "arguments": [{"autoModelProgram": 2, "autoModelValue": power, "msgType": 1, "autoModel": 8}],
                "function": "deviceAutomation",
            }
        )
        return power

    async def discharge(self, power: int) -> int:
        _LOGGER.info(f"Power discharge {self.name} => {power}")
        self.mqttInvoke(
            {
                "arguments": [{"autoModelProgram": 2, "autoModelValue": power, "msgType": 1, "autoModel": 8}],
                "function": "deviceAutomation",
            }
        )
        return power

    async def power_off(self) -> None:
        """Set the power off."""
        self.mqttInvoke(
            {
                "arguments": [{"autoModelProgram": 0, "autoModelValue": 0, "msgType": 1, "autoModel": 0}],
                "function": "deviceAutomation",
            }
        )
