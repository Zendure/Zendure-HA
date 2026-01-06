"""Devices for Zendure Integration."""

import json
import logging
from datetime import datetime
from enum import Enum
from typing import Any

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant

from .battery import ZendureBattery
from .entity import ZendureEntities, ZendureEntity
from .sensor import ZendureSensor

_LOGGER = logging.getLogger(__name__)


class ZendureSmartMeter(ZendureEntities):
    """Representation of a Zendure smart meter."""

    def __init__(self, hass: HomeAssistant, name: str, device_id: str, device_sn: str, model: str, model_id: str) -> None:
        """Initialize the smart meter device."""
        super().__init__(hass, name, model, device_id, device_sn, model_id)
        self.entityCreate()

    def entityCreate(self) -> None:
        """Create the device entities."""
        self.electricLevel = ZendureSensor(self, "power", None, "W", "power", "measurement")

    def entityRead(self, payload: dict) -> None:
        """Handle incoming MQTT message for the device."""

        def update_entity(key: str, value: Any) -> None:
            if entity := self.__dict__.get(key):
                entity.update_value(value)

        if (properties := payload.get("properties")) and len(properties) > 0:
            for key, value in properties.items():
                update_entity(key, value)

        if batprops := payload.get("packData"):
            for b in batprops:
                if (sn := b.get("sn", None)) is None:
                    continue

                if (bat := self.batteries.get(sn, None)) is None:
                    self.batteries[sn] = ZendureBattery(self.hass, self.name, sn)
                    self.kWh = sum(0 if b is None else b.kWh for b in self.batteries.values())
                elif bat and b:
                    bat.entityRead(b)

    async def entityWrite(self, entity: ZendureEntity, value: Any) -> None:
        """Write a property to the device via MQTT."""
        if entity.unique_id is None:
            _LOGGER.error(f"Entity {entity.name} has no unique_id, cannot write property {self.name}")
            return

        property_name = entity.unique_id[(len(self.name) + 1) :]
        _LOGGER.info(f"Writing property {self.name} {property_name} => {value}")
        self.mqttPublish(f"iot/{self.prodKey}/{self.deviceId}/properties/write", {"properties": {property_name: value}})

    def setStatus(self) -> None:
        """Set the device connection status."""
        try:
            if self.lastseen == datetime.min:
                self.connectionStatus.update_value(0)
            elif self.socStatus.asInt == 1:
                self.connectionStatus.update_value(1)
            elif self.hemsState.is_on:
                self.connectionStatus.update_value(2)
            elif self.fuseGroup.value == 0:
                self.connectionStatus.update_value(3)
            else:
                self.connectionStatus.update_value(10)
        except Exception:
            self.connectionStatus.update_value(0)

    def setLimits(self, charge: int, discharge: int) -> None:
        try:
            """Set the device limits."""
            self.charge_limit = charge
            self.charge_optimal = charge // 4
            self.charge_start = charge // 10
            self.inputLimit.update_range(0, abs(charge))

            self.discharge_limit = discharge
            self.discharge_optimal = discharge // 4
            self.discharge_start = discharge // 10
            self.outputLimit.update_range(0, discharge)
        except Exception:
            _LOGGER.error(f"SetLimits error {self.name} {charge} {discharge}!")

    async def setFuseGroup(self, updateFuseGroup: Any) -> None:
        """Set the device fuse group."""
        from .fusegroup import FuseGroup

        try:
            if self.fuseGroup.onchanged is None:
                self.fuseGroup.onchanged = updateFuseGroup

            self.fuseGrp = None
            match self.fuseGroup.state:
                case "owncircuit" | "group3600":
                    self.fuseGrp = FuseGroup(self.name, 3600, -3600)
                case "group800":
                    self.fuseGrp = FuseGroup(self.name, 800, -1200)
                case "group800_2400":
                    self.fuseGrp = FuseGroup(self.name, 800, -2400)
                case "group1200":
                    self.fuseGrp = FuseGroup(self.name, 1200, -1200)
                case "group2000":
                    self.fuseGrp = FuseGroup(self.name, 2000, -2000)
                case "group2400":
                    self.fuseGrp = FuseGroup(self.name, 2400, -2400)
                case "unused":
                    await self.power_off()
                case _:
                    _LOGGER.debug("Device %s has unsupported fuseGroup state: %s", self.name, self.fuseGroup.state)

        except AttributeError as err:
            _LOGGER.error("Device %s missing fuseGroup attribute: %s", self.name, err)
        except Exception as err:
            _LOGGER.error("Unable to create fusegroup for device %s (%s): %s", self.name, self.deviceId, err, exc_info=True)

    async def charge(self, _power: int) -> int:
        """Set the power output/input."""
        return 0

    async def power_charge(self, power: int) -> int:
        """Set charge power."""
        power = min(0, max(power, self.charge_limit))
        # if abs(power - self.homeInput.asInt + self.homeOutput.asInt) <= SmartMode.POWER_TOLERANCE:
        #     _LOGGER.info(f"Power charge {self.name} => no action [power {power}]")
        #     return self.homeInput.asInt
        return await self.charge(power)

    async def discharge(self, _power: int) -> int:
        """Set the power output/input."""
        return 0

    async def power_discharge(self, power: int) -> int:
        """Set discharge power."""
        power = max(0, min(power, self.discharge_limit))
        # if abs(power - self.homeOutput.asInt + self.homeInput.asInt) <= SmartMode.POWER_TOLERANCE:
        #     _LOGGER.info(f"Power discharge {self.name} => no action [power {power}]")
        #     return self.homeOutput.asInt
        return await self.discharge(power)

    async def power_off(self) -> None:
        """Set the power off."""
