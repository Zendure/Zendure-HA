"""Zendure Integration device."""

from __future__ import annotations

import json
import logging
import threading
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.number import NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.template import Template
from homeassistant.util import dt as dt_util
from homeassistant.util.async_ import run_callback_threadsafe
from paho.mqtt import client as mqtt_client

from .const import DOMAIN, ManagerState, SmartMode

_LOGGER = logging.getLogger(__name__)

CONST_FACTOR = 2
CONST_HEADER = {"content-type": "application/json; charset=UTF-8"}


class Device:
    empty = Entity()

    createEntity: dict[str, Any] = {
        "power": ("W", "power"),
        "packInputPower": ("W", "power"),
        "outputPackPower": ("W", "power"),
        "outputHomePower": ("W", "power"),
        "gridInputPower": ("W", "power"),
        "gridOffPower": ("W", "power"),
        "solarInputPower": ("W", "power"),
        "solarPower1": ("W", "power"),
        "solarPower2": ("W", "power"),
        "solarPower3": ("W", "power"),
        "solarPower4": ("W", "power"),
        "solarPower5": ("W", "power"),
        "solarPower6": ("W", "power"),
        "energyPower": ("W"),
        "inverseMaxPower": ("W"),
        "BatVolt": ("V", "voltage",100),
        "VoltWakeup": ("V", "voltage"),
        "totalVol": ("V", "voltage", 100),
        "maxVol": ("V", "voltage", 100),
        "minVol": ("V", "voltage", 100),
        "batcur": ("A", "current", 10),
        "maxTemp": ("°C", "temperature", "{{ (value | float - 2731) / 10 | round(1) }}"),
        "hyperTmp": ("°C", "temperature", "{{ (value | float - 2731) / 10 | round(1) }}"),
        "softVersion": ("version"),
        "masterSoftVersion": ("version"),
        "masterhaerVersion": ("version"),
        "dspversion": ("version"),
        "socLevel": ("%", "battery"),
        "soh": ("%", None, "{{ (value / 10) }}"),
        "electricLevel": ("%", "battery"),
        "remainOutTime": ("h", "duration"),
        "remainInputTime": ("h", "duration"),
        "masterSwitch": ("binary"),
        "buzzerSwitch": ("binary"),
        "wifiState": ("binary"),
        "heatState": ("binary"),
        "reverseState": ("binary"),
        "pass": ("binary"),
        "lowTemperature": ("binary"),
        "autoHeat": ("binary"),
        "localState": ("binary"),
        "ctOff": ("binary"),
        "lampSwitch": ("switch"),
        "invOutputPower": ("none"),
        "ambientLightNess": ("none"),
        "ambientLightColor": ("none"),
        "ambientLightMode": ("none"),
        "ambientSwitch": ("none"),
        "PowerCycle": ("none"),
        "packInputPowerCycle": ("none"),
        "outputPackPowerCycle": ("none"),
        "outputHomePowerCycle": ("none"),
    }

    def __init__(self, hass: HomeAssistant, deviceId: str, name: str, model: str, parent: str | None = None) -> None:
        """Initialize Device."""
        self._hass = hass
        self.deviceId = deviceId
        self.name = name
        self.unique = "".join(self.name.split())
        self.entities: dict[str, Entity | None] = {}
        self.attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.name)},
            name=self.name,
            manufacturer="Zendure",
            model=model,
        )

        if parent is not None:
            self.attr_device_info["via_device"] = (DOMAIN, parent)
        self.entities: dict[str, Entity | None] = {}

    async def unload(self) -> None:
        """Unload the device."""
        self.entities.clear()

    def entityUpdate(self, key: Any, value: Any) -> bool:
        from .binary_sensor import ZendureBinarySensor
        from .sensor import ZendureCalcSensor, ZendureSensor
        from .switch import ZendureSwitch

        # check if entity is already created
        if (entity := self.entities.get(key, None)) is None:
            if info := self.createEntity.get(key, None):
                match info if isinstance(info, str) else info[0]:
                    case "W":
                        entity = ZendureSensor(self, key, None, "W", "power", "measurement", None)
                    case "V":
                        factor = int(info[2]) if len(info) > CONST_FACTOR else 1
                        entity = ZendureSensor(self, key, None, "V", "voltage", "measurement", 1, factor)
                    case "%":
                        entity = ZendureSensor(self, key, None, "%", "battery", "measurement", None)
                    case "A":
                        factor = int(info[2]) if len(info) > CONST_FACTOR else 1
                        entity = ZendureSensor(self, key, None, "A", "current", "measurement", None, factor)
                    case "h":
                        tmpl = Template("{{ value | int / 60 }}", self._hass)
                        entity = ZendureSensor(self, key, tmpl, "h", "duration", "measurement", None)
                    case "°C":
                        tmpl = Template("{{ (value | float - 2731) / 10 | round(1) }}", self._hass)
                        entity = ZendureSensor(self, key, tmpl, "°C", "temperature", "measurement", None)
                    case "version":
                        entity = ZendureCalcSensor(self, key)
                        entity.calculate = entity.calculate_version
                    case "binary":
                        entity = ZendureBinarySensor(self, key, None, "switch")
                    case "switch":
                        entity = ZendureSwitch(self, key, self.entityWrite, None, "switch", value)
                    case "none":
                        self.entities[key] = entity = self.empty
                    case _:
                        _LOGGER.debug(f"Create sensor {self.name} {key} with no unit")
            else:
                entity = ZendureSensor(self, key)

            if entity is not None and entity.platform is not None:
                entity.update_value(value)
            return True

        # update entity state
        if entity is not None and entity.platform and entity.state != value:
            entity.update_value(value)
            return True

        return False

    def entityWrite(self, _entity: Entity, _value: Any) -> None:
        return

    def call_threadsafe(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        if self._hass.loop_thread_id != threading.get_ident():
            run_callback_threadsafe(self._hass.loop, func, *args, **kwargs).result()
        else:
            func(*args, **kwargs)


class ZendureBattery(Device):
    """Zendure Battery class for devices."""

    def __init__(self, hass: HomeAssistant, sn: str, parent: Device) -> None:
        """Initialize Device."""
        self.kWh = 0.0
        model = "???"
        match sn[0]:
            case "A":
                if sn[3] == "3":
                    model = "AIO2400"
                    self.kWh = 2.4
                else:
                    model = "AB1000"
                    self.kWh = 0.96
            case "B":
                model = "AB1000S"
                self.kWh = 0.96
            case "C":
                model = "AB2000" + ("S" if sn[3] == "F" else "")
                self.kWh = 1.92
            case "F":
                model = "AB3000"
                self.kWh = 2.88

        super().__init__(hass, sn, sn, model, parent.name)


class ZendureDevice(Device):
    """Zendure Device class for devices integration."""

    mqttEmpty = mqtt_client.Client()

    def __init__(self, hass: HomeAssistant, deviceId: str, name: str, model: str, definition: dict[str, str], parent: str | None = None) -> None:
        """Initialize Device."""
        super().__init__(hass, deviceId, name, model, parent)
        self.name = name
        self.prodkey = definition["productKey"]
        self.snNumber = definition["snNumber"]
        self.definition = definition

        self.lastseen = datetime.min
        self.mqtt = self.mqttEmpty
        self.ipAddress = definition.get("ip", "")
        if self.ipAddress == "":
            self.ipAddress = f"zendure-{definition['productModel'].replace(' ', '')}-{self.snNumber}.local"
        self.bleMac = ""
        self.topic_read = f"iot/{self.prodkey}/{self.deviceId}/properties/read"
        self.topic_write = f"iot/{self.prodkey}/{self.deviceId}/properties/write"
        self.topic_function = f"iot/{self.prodkey}/{self.deviceId}/function/invoke"

        self.batteries: dict[str, ZendureBattery | None] = {}
        self._messageid = 0
        self.capacity = 0
        self.powerAct = 0
        self.powerMax = 0
        self.powerMin = 0
        self.kWh = 0.0
        self.isLegacy = definition.get("isLegacy", False)

        from .number import ZendureNumber
        from .select import ZendureRestoreSelect, ZendureSelect
        from .sensor import ZendureRestoreSensor, ZendureSensor

        self.limitOutput = ZendureNumber(self, "outputLimit", self.entityWrite, None, "W", "power", 800, 0, NumberMode.SLIDER)
        self.limitInput = ZendureNumber(self, "inputLimit", self.entityWrite, None, "W", "power", 1200, 0, NumberMode.SLIDER)
        self.minSoc = ZendureNumber(self, "minSoc", self.entityWrite, None, "%", "soc", 100, 0, NumberMode.SLIDER, 10)
        self.socSet = ZendureNumber(self, "socSet", self.entityWrite, None, "%", "soc", 100, 0, NumberMode.SLIDER, 10)

        clusters = {0: "unused", 1: "clusterowncircuit", 2: "cluster800", 3: "cluster1200", 4: "cluster2400", 5: "cluster3600"}
        self.cluster = ZendureRestoreSelect(self, "cluster", clusters, None)
        self.acMode = ZendureSelect(self, "acMode", {1: "input", 2: "output"}, self.entityWrite, 1)
        self.gridReverse = ZendureSelect(self, "gridReverse", {0: "auto", 1: "on", 2: "off"}, self.entityWrite, 1)

        self.chargeTotal = ZendureRestoreSensor(self, "aggrChargeTotal", None, "kWh", "energy", "total_increasing", 2)
        self.dischargeTotal = ZendureRestoreSensor(self, "aggrDischargeTotal", None, "kWh", "energy", "total_increasing", 2)
        self.solarTotal = ZendureRestoreSensor(self, "aggrSolarTotal", None, "kWh", "energy", "total_increasing", 2)

        self.electricLevel = ZendureSensor(self, "electricLevel", None, "%", "battery", "measurement")
        self.packInputPower = ZendureSensor(self, "packInputPower", None, "W", "power", "measurement")
        self.outputPackPower = ZendureSensor(self, "outputPackPower", None, "W", "power", "measurement")
        self.solarInputPower = ZendureSensor(self, "solarInputPower", None, "W", "power", "measurement")

    def entityUpdate(self, key: Any, value: Any) -> bool:
        # update entity state
        changed = super().entityUpdate(key, value)
        if changed:
            match key:
                case "outputPackPower":
                    self.powerAct = int(value)
                    self.chargeTotal.aggregate(dt_util.now(), value)
                    self.dischargeTotal.aggregate(dt_util.now(), 0)
                case "packInputPower":
                    self.chargeTotal.aggregate(dt_util.now(), 0)
                    self.dischargeTotal.aggregate(dt_util.now(), value)
                case "solarInputPower":
                    self.solarTotal.aggregate(dt_util.now(), value)
                case "inverseMaxPower":
                    self.powerMax = value
                    self.limitOutput.update_range(0, value)

        return changed

    def entityWrite(self, entity: Entity, value: Any) -> None:
        if entity.unique_id is None:
            _LOGGER.error(f"Entity {entity.name} has no unique_id, cannot write property {self.name}")
            return

        _LOGGER.info(f"Writing property {self.name} {entity.name} => {value}")
        self._messageid += 1
        property_name = entity.unique_id[(len(self.name) + 1) :]
        payload = json.dumps(
            {
                "deviceId": self.deviceId,
                "messageId": self._messageid,
                "timestamp": int(datetime.now().timestamp()),
                "properties": {property_name: value},
            },
            default=lambda o: o.__dict__,
        )
        self.mqtt.publish(self.topic_write, payload)

    async def mqttRefresh(self) -> None:
        return

    def mqttInvoke(self, command: Any) -> None:
        self._messageid += 1
        command["messageId"] = self._messageid
        command["deviceKey"] = self.deviceId
        command["timestamp"] = int(datetime.now().timestamp())
        payload = json.dumps(command, default=lambda o: o.__dict__)

        self.mqtt.publish(self.topic_function, payload)

    def mqttMessage(self, topic: str, payload: Any) -> bool:
        try:
            match topic:
                case "properties/report":
                    self.lastseen = datetime.now() + timedelta(minutes=2)
                    if (properties := payload.get("properties", None)) and len(properties) > 0:
                        for key, value in properties.items():
                            self.entityUpdate(key, value)

                    # check for the IP address
                    if (ip := payload.get("ip", None)) is not None and self.ipAddress != ip:
                        self.ipAddress = ip
                        _LOGGER.info(f"IP address for {self.name} set to {self.ipAddress}")

                    # check for the BLE MAC address
                    if not self.bleMac and (bleMac := payload.get("bleMac", None)) is not None:
                        self.bleMac = bleMac
                        _LOGGER.info(f"BLE MAC address for {self.name} set to {self.bleMac}")

                    # update the battery properties
                    if batprops := payload.get("packData", None):
                        for b in batprops:
                            sn = b.pop("sn")

                            if (bat := self.batteries.get(sn, None)) is None:
                                if not b:
                                    self.batteries[sn] = bat = ZendureBattery(self._hass, sn, self)
                                    self.kWh += bat.kWh

                            elif bat and b:
                                for key, value in b.items():
                                    bat.entityUpdate(key, value)

                case "firmware/report":
                    _LOGGER.info(f"Firmware report for {self.name} => {payload}")
        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())

        return False

    def power_set(self, _state: ManagerState, power: int) -> int:
        """Set the power output/input."""
        return power

    def power_get(self) -> int:
        """Get the current power."""
        if not self.online or self.packInputPower.state is None or self.outputPackPower.state is None:
            return 0
        self.powerAct = self.packInputPower.value - self.outputPackPower.value
        if self.powerAct != 0:
            self.powerAct += self.solarInputPower.value
        return self.powerAct

    def power_capacity(self, state: ManagerState) -> float:
        """Get the device capacity for state."""
        if not self.online or self.electricLevel.state is None or self.socSet.state is None or self.minSoc.state is None:
            return 0.0
        if state == ManagerState.CHARGING:
            self.capacity = self.kWh * max(0, self.socSet.value - self.electricLevel.value)
        else:
            self.capacity = self.kWh * max(0, self.electricLevel.value - self.minSoc.value)

        return self.capacity

    @property
    def online(self) -> bool:
        return self.lastseen > datetime.now()


class ZendureLegacy(ZendureDevice):
    """Zendure Legacy class for devices."""

    def __init__(self, hass: HomeAssistant, deviceId: str, name: str, model: str, definition: dict[str, str], parent: str | None = None) -> None:
        """Initialize Device."""
        super().__init__(hass, deviceId, name, model, definition, parent)

    async def mqttRefresh(self) -> None:
        """Initialize MQTT connection."""
        self.mqtt.publish(self.topic_read, '{"properties": ["getAll"]}')


class ZendureZenSdk(ZendureDevice):
    """Zendure Zen SDK class for devices."""

    def __init__(self, hass: HomeAssistant, deviceId: str, name: str, model: str, definition: dict[str, str], parent: str | None = None) -> None:
        """Initialize Device."""
        super().__init__(hass, deviceId, name, model, definition, parent)
        self.session = async_get_clientsession(hass, verify_ssl=False)
        self.httpid = 0

    async def unload(self) -> None:
        """Unload the device."""
        return

    async def mqttInit(self) -> None:
        """Initialize MQTT connection."""
        if (srv := self.definition["server"]) != "":
            mqttSrv = f"mqtt://{srv}:{self.definition['port']}"
            config = await self.httpGet("rpc?method=HA.Mqtt.GetConfig")
            if config is not None and config.get("server", "") != mqttSrv:
                cmd = {
                    "sn": self.snNumber,
                    "method": "HA.Mqtt.SetConfig",
                    "params": {
                        "config": {
                            "enable": True,
                            "server": mqttSrv,
                            "username": self.definition["username"],
                            "password": self.definition["password"],
                        }
                    },
                }
                _LOGGER.debug(f"HTTP POST {self.ipAddress} => {cmd}")
                await self.httpPost("rpc", cmd)

    def entityWrite(self, entity: Entity, value: Any) -> None:
        if entity.unique_id is None:
            _LOGGER.error(f"Entity {entity.name} has no unique_id, cannot write property {self.name}")
            return

        _LOGGER.info(f"Writing property {self.name} {entity.name} => {value}")
        property_name = entity.unique_id[(len(self.name) + 1) :]
        self._hass.async_create_task(self.httpPost("properties/write", {"properties": {property_name: value}}))

    def power_set(self, state: ManagerState, power: int) -> int:
        if len(self.ipAddress) == 0:
            _LOGGER.error(f"Cannot set power for {self.name} as IP address is not set")
            return power

        delta = abs(power - self.powerAct)
        if delta <= SmartMode.IGNORE_DELTA and state != ManagerState.IDLE:
            _LOGGER.info(f"Update power {self.name} => no action [power {power}]")
            return delta

        _LOGGER.info(f"Update power {self.name} => {power} state: {state} delta: {delta}")
        if state == ManagerState.CHARGING:
            self._hass.async_create_task(self.httpPost("properties/write", {"properties": {"smartMode": 1, "acmode": 1, "inputLimit": -power}}))
        else:
            self._hass.async_create_task(self.httpPost("properties/write", {"properties": {"smartMode": 1, "acmode": 2, "outputLimit": power}}))

        return 0

    async def httpGet(self, url: str) -> Any | None:
        try:
            url = f"http://{self.ipAddress}/{url}"
            response = await self.session.get(url, headers=CONST_HEADER)
            payload = json.loads(await response.text())
            _LOGGER.debug(f"HTTP GET {self.ipAddress} {url} => {payload}")
            if data := payload.get("data"):
                return data
        except Exception as e:
            _LOGGER.error(f"Unable to connect to Zendure {e}!")
        return None

    async def httpPost(self, url: str, command: Any) -> None:
        try:
            self.httpid += 1
            command["id"] = self.httpid
            command["sn"] = self.snNumber
            url = f"http://{self.ipAddress}/{url}"
            response = await self.session.post(url, json=command, headers=CONST_HEADER)
            _LOGGER.debug(f"HTTP POST {self.ipAddress} => {response}")
        except Exception as e:
            _LOGGER.error(f"Unable to connect to Zendure {e}!")
