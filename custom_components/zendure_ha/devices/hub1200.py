"""Module for the Hub1200 device integration in Home Assistant."""

import logging

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureDevice

_LOGGER = logging.getLogger(__name__)


class Hub1200(ZendureDevice):
    def __init__(self, hass: HomeAssistant, device_id: str, device_sn: str, model: str, model_id: str) -> None:
        """Initialise Hub1200."""
        super().__init__(hass, device_id, device_sn, model, model_id, None, False, 4)
        self.setLimits(-800, 800)
        self.maxSolar = -800

    def batteryUpdate(self) -> None:
        # Check if any battery has kWh > 1
        if any(battery.kWh > 1 for battery in self.batteries.values()):
            self.setLimits(-1200, self.outputLimit.asInt)

    def power_update(self, power: int) -> int:
        if power < 0:
            self.mqttInvoke(
                {
                    "arguments": [{"autoModelProgram": 2, "autoModelValue": power, "msgType": 1, "autoModel": 8}],
                    "function": "deviceAutomation",
                }
            )
        else:
            self.mqttInvoke(
                {
                    "arguments": [{"autoModelProgram": 2, "autoModelValue": power, "msgType": 1, "autoModel": 8}],
                    "function": "deviceAutomation",
                }
            )
        return power

    def power_off(self) -> None:
        """Set the power off."""
        self.mqttInvoke(
            {
                "arguments": [{"autoModelProgram": 0, "autoModelValue": 0, "msgType": 1, "autoModel": 0}],
                "function": "deviceAutomation",
            }
        )
