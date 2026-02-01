"""Module for the Hub2000 device integration in Home Assistant."""

import logging

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.battery import ZendureBattery
from custom_components.zendure_ha.device import ZendureDevice

_LOGGER = logging.getLogger(__name__)


class Hub2000(ZendureDevice):
    def __init__(self, hass: HomeAssistant, device_id: str, device_sn: str, model: str, model_id: str) -> None:
        """Initialise Hub2000."""
        super().__init__(hass, device_id, device_sn, model, model_id, None, False, 4)
        self.setLimits(-800, 800)
        self.maxSolar = -800

    def batteryUpdate(self) -> None:
        self.setLimits(-1800 if len(self.batteries) > 1 else -1200 if self.kWh > 1 else -800, self.outputLimit.asInt)

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
