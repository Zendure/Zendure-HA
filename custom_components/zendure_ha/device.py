"""Devices for Zendure Integration."""

import json
import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.number import NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
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
    NOBATTERY = 2
    NOFUSEGROUP = 3
    ACTIVE = 4
    CALIBRATE = 5
    HEMS = 6


class ZendureDevice(ZendureEntities):
    """Representation of a Zendure device."""

    fuseGroups: dict[Any, str] = {0: "unused", 1: "owncircuit", 2: "group800", 3: "group800_2400", 4: "group1200", 5: "group2000", 6: "group2400", 7: "group3600"}

    def __init__(self, hass: HomeAssistant, device_id: str, device_sn: str, model: str, model_id: str, parent: str | None = None) -> None:
        """Initialize the Zendure device."""
        from .fusegroup import CONST_EMPTY_GROUP, FuseGroup

        super().__init__(hass, model, device_id, device_sn, model_id, parent)
        self.mqttcloud: mqtt_client.Client
        self.batteries: dict[str, ZendureBattery | None] = {}
        self.kWh = 0.0
        self.limit = [0, 0]
        self.level = 0
        self.fuseGrp: FuseGroup = CONST_EMPTY_GROUP
        self.values = [0, 0, 0, 0]
        self.power_setpoint = 0
        self.power_time = datetime.min
        self.power_offset = 0
        self.power_limit = 0
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
        self.connectionStatus = ZendureSensor(self, "connectionStatus", state=0)
        self.remainingTime = ZendureSensor(self, "remainingTime", None, "h", "duration", "measurement", hidden=True)
        self.byPass = ZendureBinarySensor(self, "pass")
        self.hemsState = ZendureBinarySensor(self, "hemsState")
        self.fuseGroup = ZendureRestoreSelect(self, "fuseGroup", self.fuseGroups, None)

        self.aggrCharge = ZendureRestoreSensor(self, "aggrChargeTotal", None, "kWh", "energy", "total_increasing", 2)
        self.aggrDischarge = ZendureRestoreSensor(self, "aggrDischargeTotal", None, "kWh", "energy", "total_increasing", 2)
        self.aggrHomeInput = ZendureRestoreSensor(self, "aggrHomeInputTotal", None, "kWh", "energy", "total_increasing", 2)
        self.aggrHomeOut = ZendureRestoreSensor(self, "aggrHomeOutputTotal", None, "kWh", "energy", "total_increasing", 2)
        self.aggrSolar = ZendureRestoreSensor(self, "aggrSolarTotal", None, "kWh", "energy", "total_increasing", 2)
        self.aggrSwitchCount = ZendureRestoreSensor(self, "switchCount", None, None, None, "total_increasing", 0)
        self.aggrOffGrid: ZendureRestoreSensor | None = None

    def entityRead(self, payload: dict) -> None:
        """Handle incoming MQTT message for the device."""
        if (properties := payload.get("properties")) and len(properties) > 0:
            for key, value in properties.items():
                self.entityUpdate(key, value)

        if batprops := payload.get("packData"):
            for b in batprops:
                if (sn := b.get("sn", None)) is None:
                    continue

                if (bat := self.batteries.get(sn, None)) is None:
                    self.batteries[sn] = ZendureBattery(self.hass, self.name, sn)
                    self.kWh = sum(0 if b is None else b.kWh for b in self.batteries.values())
                    self.batteryUpdate()
                elif bat and b:
                    bat.entityRead(b)

    def batteryUpdate(self) -> None:
        """Update device based on battery status."""

    def entityUpdate(self, key: str, value: Any) -> None:
        def home(value: int) -> None:
            if self.power_time > datetime.min and abs(value - self.power_setpoint) < 20:
                self.power_time = datetime.min
            self.homePower.update_value(value)

        match key:
            case "gridInputPower":
                self.values[0] = value
                self.homePower.update_value(-value + self.values[1])
                self.aggrHomeInput.aggregate(dt_util.now(), value)
            case "outputHomePower":
                self.values[1] = value
                home(-self.values[0] + value)
                self.aggrHomeOut.aggregate(dt_util.now(), value)
            case "outputPackPower":
                self.values[2] = value
                home(-value + self.values[3])
                self.aggrCharge.aggregate(dt_util.now(), value)
                self.aggrDischarge.aggregate(dt_util.now(), 0)
            case "packInputPower":
                self.values[3] = value
                self.batteryPower.update_value(-self.values[2] + value)
                self.aggrCharge.aggregate(dt_util.now(), 0)
                self.aggrDischarge.aggregate(dt_util.now(), value)
            case "solarInputPower":
                self.solarPower.update_value(value)
                self.aggrSolar.aggregate(dt_util.now(), value)
            case "gridOffPower":
                if self.aggrOffGrid is not None and self.offGrid is not None:
                    self.offGrid.update_value(value)
                    self.aggrOffGrid.aggregate(dt_util.now(), value)
            case "electricLevel":
                self.electricLevel.update_value(value)
                self.level = self.electricLevel.asNumber - self.minSoc.asNumber
                self.availableKwh.update_value(round(self.kWh * self.level / 100, 2))
            case "remainOutTime" | "remainInputTime":
                self.remainingTime.update_value(self.calcRemainingTime())
            case "inverseMaxPower":
                self.setLimits(self.inputLimit.asInt, value)
            case "chargeLimit" | "chargeMaxLimit":
                self.setLimits(-value, self.outputLimit.asInt)
            case "hemsState":
                self.hemsState.update_value(value)
                self.setStatus()
            case _:
                if entity := self.__dict__.get(key):
                    entity.update_value(value)

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
        self.ready = datetime.now() + timedelta(seconds=1)

    def mqttWrite(self, command: Any) -> None:
        self.mqttPublish(self.topic_write, command)
        self.ready = datetime.now() + timedelta(seconds=1)

    def mqttRegister(self, payload: dict) -> None:
        """Handle device registration."""
        if (params := payload.get("params")) is not None and (token := params.get("token")) is not None:
            self.mqttPublish(f"iot/{self.prodKey}/{self.deviceId}/register/replay", {"token": token, "result": 0})
        else:
            _LOGGER.warning(f"MQTT register failed for device {self.name}: no token in payload")

    def refresh(self) -> None:
        self.setStatus()
        self.mqttPublish(self.topic_read, {"properties": ["getAll"]})

    @property
    def status(self) -> DeviceState:
        return DeviceState(self.connectionStatus.asInt)

    def setStatus(self, lastseen: datetime | None = None) -> None:
        """Set the device connection status."""
        try:
            if lastseen is not None:
                self.lastseen = lastseen

            if self.lastseen <= datetime.now() - timedelta(minutes=2):
                self.connectionStatus.update_value(DeviceState.OFFLINE.value)
            elif self.hemsState.is_on:
                self.connectionStatus.update_value(DeviceState.HEMS.value)
            elif self.kWh == 0.0:
                self.connectionStatus.update_value(DeviceState.NOBATTERY.value)
            elif self.fuseGroup.value == 0:
                self.connectionStatus.update_value(DeviceState.NOFUSEGROUP.value)
            else:
                self.connectionStatus.update_value(DeviceState.ACTIVE.value)
        except Exception:
            self.connectionStatus.update_value(DeviceState.OFFLINE.value)

    def setLimits(self, charge: int, discharge: int) -> None:
        try:
            """Set the device limits."""
            self.limit = [charge, discharge]
            self.inputLimit.update_range(0, abs(charge))
            self.outputLimit.update_range(0, discharge)
        except Exception:
            _LOGGER.error(f"SetLimits error {self.name} {charge} {discharge}!")

    def setFuseGroup(self, updateFuseGroup: Any) -> None:
        """Set the device fuse group."""
        from .fusegroup import FuseGroup

        try:
            if self.fuseGroup.onchanged is None:
                self.fuseGroup.onchanged = updateFuseGroup

            self.fuseGrp = self.emptyFuseGroup
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
                    self.power_off()
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
            return self.power_setpoint

        if (delta := abs(pwr - self.homePower.asInt)) <= SmartMode.POWER_TOLERANCE:
            return self.homePower.asInt + self.power_offset

        pwr = min(max(self.limit[0], pwr), self.limit[1])
        self.power_setpoint = pwr
        self.power_time = time + timedelta(seconds=3 + delta / 250)

        pwr = self.power_update(pwr)
        return pwr + self.power_offset

    def power_off(self) -> None:
        """Set the power off."""
        self.mqttWrite({"properties": {"smartMode": 0, "acMode": 1, "outputLimit": 0, "inputLimit": 0}})

    def power_update(self, power: int) -> int:
        """Set the power output/input."""
        if power >= 0:
            self.mqttWrite({"properties": {"smartMode": 0 if power == 0 else 1, "acMode": 2, "outputLimit": power, "inputLimit": 0}})
        else:
            self.mqttWrite({"properties": {"smartMode": 0 if power == 0 else 1, "acMode": 1, "outputLimit": 0, "inputLimit": -power}})
        return power

    def calcRemainingTime(self) -> float:
        """Calculate the remaining time."""
        level = self.electricLevel.asInt
        power = self.batteryPower.asInt

        if power == 0:
            return 0

        if power < 0:
            soc = self.socSet.asNumber
            return 0 if level >= soc else min(999, self.kWh * 10 / -power * (soc - level))

        soc = self.minSoc.asNumber
        return 0 if level <= soc else min(999, self.kWh * 10 / power * (level - soc))
