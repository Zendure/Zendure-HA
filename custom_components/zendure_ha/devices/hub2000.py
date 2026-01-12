"""Module for the Hyper2000 device integration in Home Assistant."""

import logging

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.battery import ZendureBattery
from custom_components.zendure_ha.device import ZendureDevice

_LOGGER = logging.getLogger(__name__)


class Hub2000(ZendureDevice):
    def __init__(self, hass: HomeAssistant, device_id: str, device_sn: str, model: str, model_id: str) -> None:
        """Initialise Hub2000."""
        super().__init__(hass, device_id, device_sn, model, model_id)
        self.setLimits(-800, 800)
        self.maxSolar = -800

    def batteryUpdate(self, batteries: list[ZendureBattery]) -> None:
        self.powerMin = -1800 if len(batteries) > 1 else -1200 if batteries[0].kWh > 1 else -800
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
