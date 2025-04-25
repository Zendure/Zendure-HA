"""Module for the Hyper2000 device integration in Home Assistant."""

import logging

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.sensor import ZendureSensor
from custom_components.zendure_ha.zenduredevice import ZendureDevice, ZendureDeviceDefinition

_LOGGER = logging.getLogger(__name__)


class AB1000(ZendureDevice):
    def __init__(
        self,
        hass: HomeAssistant,
        h_id: str,
        definition: ZendureDeviceDefinition,
        parent: ZendureDevice
    ) -> None:
        """Initialise AB1000."""
        super().__init__(hass, h_id, definition, "AB 1000", parent)

    def sensorsCreate(self) -> None:
        super().sensorsCreate()

        sensors = [
            self.sensor("totalVol", "{{ (value / 100) }}", "V", "voltage", "measurement"),
            self.sensor("maxVol", "{{ (value / 100) }}", "V", "voltage", "measurement"),
            self.sensor("minVol", "{{ (value / 100) }}", "V", "voltage", "measurement"),
            self.sensor("batcur", "{{ (value / 10) }}", "A", "current", "measurement"),
            self.sensor("state"),
            self.sensor("power", None, "W", "power", "measurement"),
            self.sensor("socLevel", None, "%", "battery", "measurement"),
            self.sensor("maxTemp", "{{ (value | float/10 - 273.15) | round(2) }}", "°C", "temperature", "measurement"),
            self.sensor("softVersion"),
        ]
        ZendureSensor.addSensors(sensors)
