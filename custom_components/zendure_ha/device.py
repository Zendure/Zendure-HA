"""Devices for Zendure Integration."""

import json
import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.number import NumberMode
from homeassistant.core import HomeAssistant
from paho.mqtt import client as mqtt_client

from .battery import ZendureBattery
from .binary_sensor import ZendureBinarySensor
from .const import SmartMode
from .entity import ZendureEntities, ZendureEntity
from .number import ZendureNumber
from .select import ZendureRestoreSelect, ZendureSelect
from .sensor import ZendureRestoreSensor, ZendureSensor

_LOGGER = logging.getLogger(__name__)


class DeviceState(Enum):
    CREATED = 0
    OFFLINE = 1
    NOFUSEGROUP = 2
    ACTIVE = 3
    CALIBRATE = 4
    HEMS = 5


class ZendureDevice(ZendureEntities):
    """Representation of a Zendure device."""

    fuseGroups: dict[Any, str] = {0: "unused", 1: "owncircuit", 2: "group800", 3: "group800_2400", 4: "group1200", 5: "group2000", 6: "group2400", 7: "group3600"}

    def __init__(self, hass: HomeAssistant, device_id: str, device_sn: str, model: str, model_id: str, parent: str | None = None) -> None:
        """Initialize the Zendure device."""
        from .fusegroup import FuseGroup

        super().__init__(hass, model, device_id, device_sn, model_id, parent)
        self.mqttcloud: mqtt_client.Client
        self.batteries: dict[str, ZendureBattery | None] = {}
        self.kWh = 0.0
        self.limit = [0, 0]
        self.level = 0
        self.fuseGrp: FuseGroup | None = None
        self.values = [0, 0, 0, 0]
        self.power_setpoint = 0
        self.power_time = datetime.min
        self.power_offset = 0
        self.entityCreate()

    def entityCreate(self) -> None:
        """Create the device entities."""
        self.electricLevel = ZendureSensor(self, "electricLevel", None, "%", "battery", "measurement")
        self.homePower = ZendureSensor(self, "homePower", None, "W", "power", "measurement")
        self.batteryPower = ZendureSensor(self, "batteryPower", None, "W", "power", "measurement")
        self.solarPower = ZendureSensor(self, "solarPower", None, "W", "power", "measurement", icon="mdi:solar-panel")
        self.offGrid: ZendureSensor | None = None

        self.socStatus = ZendureSensor(self, "socStatus", state=0)
        self.socLimit = ZendureSensor(self, "socLimit", state=0)

        self.minSoc = ZendureNumber(self, "socMin", self.entityWrite, None, "%", "soc", 100, 0, NumberMode.SLIDER, 10)
        self.socSet = ZendureNumber(self, "socMax", self.entityWrite, None, "%", "soc", 100, 0, NumberMode.SLIDER, 10)
        self.acMode = ZendureSelect(self, "acMode", {1: "input", 2: "output"}, self.entityWrite, 1)
        self.inputLimit = ZendureNumber(self, "inputLimit", self.entityWrite, None, "W", "power", self.limit[0], 0, NumberMode.SLIDER)
        self.outputLimit = ZendureNumber(self, "outputLimit", self.entityWrite, None, "W", "power", self.limit[1], 0, NumberMode.SLIDER)

        self.hyperTmp = ZendureSensor(self, "Temp", ZendureSensor.temp, "°C", "temperature", "measurement")
        self.availableKwh = ZendureSensor(self, "available_kwh", None, "kWh", "energy", None, 1)
        self.connectionStatus = ZendureSensor(self, "connectionStatus")
        self.remainingTime = ZendureSensor(self, "remainingTime", None, "h", "duration", "measurement")
        self.remainingTime.hidden = True
        self.byPass = ZendureBinarySensor(self, "pass")
        self.hemsState = ZendureBinarySensor(self, "hemsState")
        self.fuseGroup = ZendureRestoreSelect(self, "fuseGroup", self.fuseGroups, None)

        self.aggrCharge = ZendureRestoreSensor(self, "aggrChargeTotal", None, "kWh", "energy", "total_increasing", 2)
        self.aggrDischarge = ZendureRestoreSensor(self, "aggrDischargeTotal", None, "kWh", "energy", "total_increasing", 2)
        self.aggrHomeInput = ZendureRestoreSensor(self, "aggrGridInputPowerTotal", None, "kWh", "energy", "total_increasing", 2)
        self.aggrHomeOut = ZendureRestoreSensor(self, "aggrOutputHomeTotal", None, "kWh", "energy", "total_increasing", 2)
        self.aggrSolar = ZendureRestoreSensor(self, "aggrSolarTotal", None, "kWh", "energy", "total_increasing", 2)
        self.aggrSwitchCount = ZendureRestoreSensor(self, "switchCount", None, None, None, "total_increasing", 0)

    def entityRead(self, payload: dict) -> None:
        """Handle incoming MQTT message for the device."""

        def update_entity(key: str, value: Any) -> None:
            match key:
                case "gridInputPower":
                    self.values[0] = value
                    self.homePower.update_value(-value + self.values[1])
                case "outputHomePower":
                    self.values[1] = value
                    self.homePower.update_value(-self.values[0] + value)
                case "outputPackPower":
                    self.values[2] = value
                    self.batteryPower.update_value(-value + self.values[3])
                case "packInputPower":
                    self.values[3] = value
                    self.batteryPower.update_value(-self.values[2] + value)
                case "solarInputPower":
                    self.solarPower.update_value(value)
                case "electricLevel":
                    self.electricLevel.update_value(value)
                    self.level = (self.electricLevel.asNumber - self.minSoc.asNumber) / 100
                    self.availableKwh.update_value(self.kWh * self.level)
                case _:
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
        self.mqttWrite({"properties": {property_name: value}})

    def mqttPublish(self, topic: str, command: Any) -> None:
        self._messageid += 1
        command["messageId"] = self._messageid
        command["deviceId"] = self.deviceId
        command["timestamp"] = int(datetime.now().timestamp())
        payload = json.dumps(command, default=lambda o: o.__dict__)
        mqtt.publish(self.hass, topic, payload=payload)

    def mqttInvoke(self, command: Any) -> None:
        self.mqttPublish(self.topic_function, command)

    def mqttWrite(self, command: Any) -> None:
        self.mqttPublish(self.topic_write, command)

    def mqttRegister(self, payload: dict) -> None:
        """Handle device registration."""
        if (params := payload.get("params")) is not None and (token := params.get("token")) is not None:
            self.mqttPublish(f"iot/{self.prodKey}/{self.deviceId}/register/replay", {"token": token, "result": 0})
        else:
            _LOGGER.warning(f"MQTT register failed for device {self.name}: no token in payload")

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
            self.limit = [charge, discharge]
            self.inputLimit.update_range(0, abs(charge))
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

    def distribute(self, power: int) -> int:
        """Set charge/discharge power, but correct for power offset."""
        pwr = power - self.power_offset
        if (time := datetime.now()) < self.power_time:
            _LOGGER.info(f"Power set ===> setpoint {self.name} => power {power}-{self.power_setpoint}")
            return self.power_setpoint

        _LOGGER.info(f"Power set {self.name} => power {power}")
        if (delta := abs(pwr - self.homePower.asInt)) <= SmartMode.POWER_TOLERANCE:
            return self.homePower.asInt + self.power_offset
        pwr = min(max(self.limit[0], pwr), self.limit[1])
        self.power_setpoint = pwr
        self.power_time = time + timedelta(seconds=1 + delta / 250)

        pwr = self.power_update(pwr)
        return pwr + self.power_offset

    async def power_off(self) -> None:
        """Set the power off."""
        self.mqttWrite({"properties": {"smartMode": 0, "acMode": 1, "outputLimit": 0, "inputLimit": 0}})

    def power_update(self, power: int) -> int:
        """Set the power output/input."""
        self.mqttWrite({"properties": {"smartMode": 0 if power == 0 else 1, "acMode": 1 if power >= 0 else 2, "outputLimit": max(0, power), "inputLimit": min(0, power)}})
        return power
