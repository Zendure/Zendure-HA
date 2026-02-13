"""Devices for Zendure Integration."""

import logging
from os import name
from typing import Any

from homeassistant.core import HomeAssistant

from .entity import ZendureEntities, ZendureEntity
from .sensor import ZendureSensor

_LOGGER = logging.getLogger(__name__)


class ZendureSmartMeter(ZendureEntities):
    """Representation of a Zendure smart meter."""

    def __init__(self, hass: HomeAssistant, device_id: str, device_sn: str, model: str, model_id: str) -> None:
        """Initialize the smart meter device."""
        super().__init__(hass, model, device_id, device_sn, model_id)
        self.entityCreate()

    def entityCreate(self) -> None:
        """Create the device entities."""
        self.totalPower = ZendureSensor(self, "power", None, "W", "power", "measurement")
        self.phase1_power = ZendureSensor(self, "phase1_power", None, "W", "power", "measurement")
        self.phase2_power = ZendureSensor(self, "phase2_power", None, "W", "power", "measurement")
        self.phase3_power = ZendureSensor(self, "phase3_power", None, "W", "power", "measurement")
        self.phase1_current = ZendureSensor(self, "phase1_current", None, "A", "current", "measurement", factor=100)
        self.phase2_current = ZendureSensor(self, "phase2_current", None, "A", "current", "measurement", factor=100)
        self.phase3_current = ZendureSensor(self, "phase3_current", None, "A", "current", "measurement", factor=100)
        self.phase1_voltage = ZendureSensor(self, "phase1_voltage", None, "V", "voltage", "measurement", factor=100)
        self.phase2_voltage = ZendureSensor(self, "phase2_voltage", None, "V", "voltage", "measurement", factor=100)
        self.phase3_voltage = ZendureSensor(self, "phase3_voltage", None, "V", "voltage", "measurement", factor=100)

    async def entityRead(self, payload: dict) -> None:
        """Handle incoming MQTT message for the device."""

        def update_entity(key: str, value: Any) -> None:
            if entity := self.__dict__.get(key):
                entity.update_value(value)

        if (properties := payload.get("properties")) and len(properties) > 0:
            for key, value in properties.items():
                update_entity(key, value)

        if (circuits := payload.get("circuits")) and len(circuits) > 0:
            for phase in circuits:
                name = f"phase{phase.get('phase')}"
                update_entity(name + "_power", phase.get("p"))
                update_entity(name + "_current", phase.get("i"))
                update_entity(name + "_voltage", phase.get("u"))

    async def entityWrite(self, entity: ZendureEntity, value: Any) -> None:
        """Write a property to the device via MQTT."""
        if entity.unique_id is None:
            _LOGGER.error(f"Entity {entity.name} has no unique_id, cannot write property {self.name}")
            return

        property_name = entity.unique_id[(len(self.name) + 1) :]
        _LOGGER.info(f"Writing property {self.name} {property_name} => {value}")
        self.mqttPublish(f"iot/{self.prodKey}/{self.deviceId}/properties/write", {"properties": {property_name: value}})
