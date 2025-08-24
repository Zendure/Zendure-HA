"""Zendure Integration device."""

from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timedelta
from typing import Any

from bleak import BleakClient
from bleak.exc import BleakError
from homeassistant.components import bluetooth, persistent_notification
from homeassistant.components.number import NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from paho.mqtt import client as mqtt_client

from .binary_sensor import ZendureBinarySensor
from .button import ZendureButton
from .const import ManagerState, SmartMode
from .entity import EntityDevice, EntityZendure
from .fusegroup import FuseGroup
from .number import ZendureNumber
from .select import ZendureRestoreSelect, ZendureSelect
from .sensor import ZendureRestoreSensor, ZendureSensor

_LOGGER = logging.getLogger(__name__)

CONST_HEADER = {"content-type": "application/json; charset=UTF-8"}
SF_COMMAND_CHAR = "0000c304-0000-1000-8000-00805f9b34fb"


class ZendureBattery(EntityDevice):
    """Zendure Battery class for devices."""

    def __init__(self, hass: HomeAssistant, sn: str, parent: EntityDevice) -> None:
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
        self.attr_device_info["serial_number"] = sn


class ZendureDevice(EntityDevice):
    """Zendure Device class for devices integration."""

    def __init__(self, hass: HomeAssistant, deviceId: str, name: str, model: str, definition: dict[str, str], parent: str | None = None) -> None:
        """Initialize Device."""
        super().__init__(hass, deviceId, name, model, parent)
        self.name = name
        self.prodkey = definition["productKey"]
        self.snNumber = definition["snNumber"]
        self.attr_device_info["serial_number"] = self.snNumber
        self.definition = definition

        self.lastseen = datetime.min
        self.mqtt: mqtt_client.Client | None = None
        self.zendure: mqtt_client.Client | None = None
        self.ipAddress = definition.get("ip", "")
        if self.ipAddress == "":
            self.ipAddress = f"zendure-{definition['productModel'].replace(' ', '')}-{self.snNumber}.local"

        self.topic_read = f"iot/{self.prodkey}/{self.deviceId}/properties/read"
        self.topic_write = f"iot/{self.prodkey}/{self.deviceId}/properties/write"
        self.topic_function = f"iot/{self.prodkey}/{self.deviceId}/function/invoke"

        self.batteries: dict[str, ZendureBattery | None] = {}
        self._messageid = 0
        self.capacity = 0
        self.powerAct = 0
        self.powerMax = 0
        self.powerMin = 0
        self.powerKwh = 0
        self.powerPct = 0
        self.powerAvail = 0
        self.kWh = 0.0

        self.limitOutput = ZendureNumber(self, "outputLimit", self.entityWrite, None, "W", "power", 800, 0, NumberMode.SLIDER)
        self.limitInput = ZendureNumber(self, "inputLimit", self.entityWrite, None, "W", "power", 1200, 0, NumberMode.SLIDER)
        self.minSoc = ZendureNumber(self, "minSoc", self.entityWrite, None, "%", "soc", 100, 0, NumberMode.SLIDER, 10)
        self.socSet = ZendureNumber(self, "socSet", self.entityWrite, None, "%", "soc", 100, 0, NumberMode.SLIDER, 10)
        self.socStatus = ZendureSensor(self, "socStatus", state=0)
        self.socLimit = ZendureSensor(self, "socLimit", state=0)

        self.fusegroup: FuseGroup | None = None
        fuseGroups = {0: "unused", 1: "owncircuit", 2: "group800", 3: "group1200", 4: "group2000", 5: "group2400", 6: "group3600"}
        self.fuseGroup = ZendureRestoreSelect(self, "fuseGroup", fuseGroups, None)
        self.acMode = ZendureSelect(self, "acMode", {1: "input", 2: "output"}, self.entityWrite, 1)

        self.chargeTotal = ZendureRestoreSensor(self, "aggrChargeTotal", None, "kWh", "energy", "total_increasing", 2)
        self.dischargeTotal = ZendureRestoreSensor(self, "aggrDischargeTotal", None, "kWh", "energy", "total_increasing", 2)
        self.solarTotal = ZendureRestoreSensor(self, "aggrSolarTotal", None, "kWh", "energy", "total_increasing", 2)
        self.switchCount = ZendureRestoreSensor(self, "switchCount", None, None, None, "total_increasing", 0)

        self.electricLevel = ZendureSensor(self, "electricLevel", None, "%", "battery", "measurement")
        self.packInputPower = ZendureSensor(self, "packInputPower", None, "W", "power", "measurement")
        self.outputPackPower = ZendureSensor(self, "outputPackPower", None, "W", "power", "measurement")
        self.solarInputPower = ZendureSensor(self, "solarInputPower", None, "W", "power", "measurement")
        self.gridInputPower = ZendureSensor(self, "gridInputPower", None, "W", "power", "measurement")
        self.outputHomePower = ZendureSensor(self, "outputHomePower", None, "W", "power", "measurement")
        self.hemsState = ZendureBinarySensor(self, "hemsState")
        self.availableKwh = ZendureSensor(self, "available_kwh", None, "kWh", "energy", None, 1)
        self.connectionStatus = ZendureSensor(self, "connectionStatus")
        self.connection: ZendureRestoreSelect
        self.remainingTime = ZendureSensor(self, "remainingTime", None, "h", "duration", "measurement")

    def setStatus(self) -> None:
        from .api import Api

        try:
            if self.lastseen == datetime.min:
                self.connectionStatus.update_value(0)
            elif self.hemsState.state == "on":
                self.connectionStatus.update_value(1)
            elif self.fuseGroup.value == 0:
                self.connectionStatus.update_value(2)
            elif self.connection.value == SmartMode.ZENSDK:
                self.connectionStatus.update_value(12)
            elif self.mqtt is not None and self.mqtt.host == Api.localServer:
                self.connectionStatus.update_value(11)
            else:
                self.connectionStatus.update_value(10)
        except Exception:
            self.connectionStatus.update_value(0)

    def entityUpdate(self, key: Any, value: Any) -> bool:
        # update entity state
        if key in {"remainOutTime", "remainInputTime"}:
            self.remainingTime.update_value(self.calcRemainingTime())
            return True

        changed = super().entityUpdate(key, value)
        try:
            if changed:
                match key:
                    case "outputPackPower":
                        if value == 0:
                            self.switchCount.update_value(1 + self.switchCount.asNumber)
                        self.chargeTotal.aggregate(dt_util.now(), value)
                        self.dischargeTotal.aggregate(dt_util.now(), 0)
                    case "packInputPower":
                        if value == 0:
                            self.switchCount.update_value(1 + self.switchCount.asNumber)
                        self.chargeTotal.aggregate(dt_util.now(), 0)
                        self.dischargeTotal.aggregate(dt_util.now(), value)
                    case "solarInputPower":
                        self.solarTotal.aggregate(dt_util.now(), value)
                    case "inverseMaxPower":
                        self.powerMax = value
                        self.limitOutput.update_range(0, value)
                    case "chargeLimit" | "chargeMaxLimit":
                        self.powerMin = -value
                        self.limitInput.update_range(0, value)
                    case "hemsState":
                        self.setStatus()
                    case "electricLevel" | "minSoc" | "socLimit":
                        self.availableKwh.update_value((self.electricLevel.asNumber - self.minSoc.asNumber) / 100 * self.kWh)
        except Exception as e:
            _LOGGER.error(f"EntityUpdate error {self.name} {key} {e}!")
            _LOGGER.error(traceback.format_exc())

        return changed

    def calcRemainingTime(self) -> float:
        """Calculate the remaining time."""
        level = self.electricLevel.asInt
        power = self.packInputPower.asInt - self.outputPackPower.asInt
        if power == 0:
            return 0

        if power < 0:
            soc = self.socSet.asNumber
            return 0 if level >= soc else min(999, self.kWh * 10 / -power * (soc - level))

        soc = self.minSoc.asNumber
        return 0 if level <= soc else min(999, self.kWh * 10 / power * (level - soc))

    async def entityWrite(self, entity: EntityZendure, value: Any) -> None:
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
        if self.mqtt is not None:
            self.mqtt.publish(self.topic_write, payload)

    async def button_press(self, _key: str) -> None:
        return

    def mqttPublish(self, topic: str, command: Any, client: mqtt_client.Client | None = None) -> None:
        command["messageId"] = self._messageid
        command["deviceKey"] = self.deviceId
        command["timestamp"] = int(datetime.now().timestamp())
        payload = json.dumps(command, default=lambda o: o.__dict__)

        if client is not None:
            client.publish(topic, payload)
        elif self.mqtt is not None:
            self.mqtt.publish(topic, payload)

    def mqttInvoke(self, command: Any) -> None:
        self._messageid += 1
        command["messageId"] = self._messageid
        command["deviceKey"] = self.deviceId
        command["timestamp"] = int(datetime.now().timestamp())
        self.mqttPublish(self.topic_function, command)

    def mqttProperties(self, payload: Any) -> None:
        if self.lastseen == datetime.min:
            self.lastseen = datetime.now() + timedelta(minutes=5)
            self.setStatus()
        else:
            self.lastseen = datetime.now() + timedelta(minutes=5)

        if (properties := payload.get("properties", None)) and len(properties) > 0:
            for key, value in properties.items():
                self.entityUpdate(key, value)

        # update the battery properties
        if batprops := payload.get("packData", None):
            for b in batprops:
                sn = b.pop("sn")

                if (bat := self.batteries.get(sn, None)) is None:
                    if not b:
                        self.batteries[sn] = ZendureBattery(self.hass, sn, self)
                        self.kWh = sum(0 if b is None else b.kWh for b in self.batteries.values())

                elif bat and b:
                    for key, value in b.items():
                        bat.entityUpdate(key, value)

    def mqttMessage(self, topic: str, payload: Any) -> bool:
        try:
            match topic:
                case "properties/report":
                    self.mqttProperties(payload)

                case "register/replay":
                    _LOGGER.info(f"Register replay for {self.name} => {payload}")
                    if self.mqtt is not None:
                        self.mqtt.publish(f"iot/{self.prodkey}/{self.deviceId}/register/replay", None, 1, True)

                case "time-sync":
                    return True

                # case "firmware/report":
                #     _LOGGER.info(f"Firmware report for {self.name} => {payload}")
                case _:
                    return False
        except Exception as err:
            _LOGGER.error(err)

        return True

    async def mqttSelect(self, _select: ZendureRestoreSelect, _value: Any) -> None:
        from .api import Api

        self.mqtt = None
        if self.lastseen != datetime.min:
            if self.connection.value == 0:
                await self.bleMqtt(Api.mqttCloud)
            elif self.connection.value == 1:
                await self.bleMqtt(Api.mqttLocal)

        _LOGGER.debug(f"Mqtt selected {self.name}")

    @property
    def bleMac(self) -> str | None:
        if (conn := self.attr_device_info.get("connections", None)) is not None:
            for connection_type, mac_address in conn:
                if connection_type == "bluetooth":
                    return mac_address
        return None

    async def bleMqtt(self, mqtt: mqtt_client.Client) -> bool:
        """Set the MQTT server for the device via BLE."""
        from .api import Api

        msg: str | None = None
        try:
            if Api.wifipsw == "" or Api.wifissid == "":
                msg = "No WiFi credentials or connections found"
                return False

            if (ble_mac := self.bleMac) is None:
                msg = "No BLE MAC address available"
                return False

            # get the bluetooth device
            if (device := bluetooth.async_ble_device_from_address(self.hass, ble_mac, True)) is None:
                msg = f"BLE device {ble_mac} not found"
                return False

            try:
                _LOGGER.info(f"Set mqtt {self.name} to {mqtt.host}")
                async with BleakClient(device) as client:
                    try:
                        await self.bleCommand(
                            client,
                            {
                                "iotUrl": mqtt.host,
                                "messageId": 1002,
                                "method": "token",
                                "password": Api.wifipsw,
                                "ssid": Api.wifissid,
                                "timeZone": "GMT+01:00",
                                "token": "abcdefgh",
                            },
                        )

                        await self.bleCommand(
                            client,
                            {
                                "messageId": 1003,
                                "method": "station",
                            },
                        )
                    finally:
                        await client.disconnect()
            except TimeoutError:
                msg = "Timeout when trying to connect to the BLE device"
                _LOGGER.warning(msg)
            except (AttributeError, BleakError) as err:
                msg = f"Could not connect to {self.name}: {err}"
                _LOGGER.warning(msg)
            except Exception as err:
                msg = f"BLE error: {err}"
                _LOGGER.warning(msg)
            else:
                self.mqtt = mqtt
                if self.zendure is not None:
                    self.zendure.loop_stop()
                    self.zendure.disconnect()
                    self.zendure = None

                self.mqttPublish(self.topic_read, {"properties": ["getAll"]}, self.mqtt)
                self.setStatus()

                return True
            return False

        finally:
            if msg is not None:
                msg = f"Error setting the MQTT server on {self.name} to {mqtt.host}, {msg}"
            else:
                msg = f"Changing the MQTT server on {self.name} to {mqtt.host} was successful"

            persistent_notification.async_create(
                self.hass,
                (msg),
                "Zendure",
                "zendure_ha",
            )

            _LOGGER.info("BLE update ready")

    async def bleCommand(self, client: BleakClient, command: Any) -> None:
        try:
            self._messageid += 1
            payload = json.dumps(command, default=lambda o: o.__dict__)
            b = bytearray()
            b.extend(map(ord, payload))
            _LOGGER.info(f"BLE command: {self.name} => {payload}")
            await client.write_gatt_char(SF_COMMAND_CHAR, b, response=False)
        except Exception as err:
            _LOGGER.warning(f"BLE error: {err}")

    def power_limit(self, state: ManagerState) -> bool:
        """Check if device is on the power limit."""
        match state:
            case ManagerState.CHARGING:
                return self.electricLevel.asNumber >= self.socSet.asNumber or self.socLimit.asNumber == 1
            case ManagerState.DISCHARGING:
                return self.electricLevel.asNumber <= self.minSoc.asNumber or self.socLimit.asNumber == 2
        return False

    def power_set(self, _state: ManagerState, _power: int) -> int:
        """Set the power output/input."""
        return 0

    async def power_get(self) -> int:
        """Get the current power."""
        # return zero if device is offline or states are unknown
        if not self.online or self.outputHomePower.state is None:
            return 0
        self.powerAct = self.outputHomePower.asInt if self.gridInputPower.state is None else self.outputHomePower.asInt - self.gridInputPower.asInt 
        return self.powerAct

    @property
    def online(self) -> bool:
        try:
            if self.lastseen < datetime.now():
                self.lastseen = datetime.min
                self.setStatus()

            return self.connectionStatus.state >= SmartMode.CONNECTED and self.socStatus.state != 1  # noqa: TRY300
        except Exception:  # pylint: disable=broad-except
            return False


class ZendureLegacy(ZendureDevice):
    """Zendure Legacy class for devices."""

    def __init__(self, hass: HomeAssistant, deviceId: str, name: str, model: str, definition: dict[str, str], parent: str | None = None) -> None:
        """Initialize Device."""
        super().__init__(hass, deviceId, name, model, definition, parent)
        self.connection = ZendureRestoreSelect(self, "connection", {0: "cloud", 1: "local"}, self.mqttSelect, 0)
        self.mqttReset = ZendureButton(self, "mqttReset", self.button_press)

    async def button_press(self, button: ZendureButton) -> None:
        from .api import Api

        match button.translation_key:
            case "mqtt_reset":
                if self.mqtt is not None:
                    _LOGGER.info(f"Resetting MQTT for {self.name}")
                    await self.bleMqtt(Api.mqttCloud if self.connection.value == 0 else Api.mqttLocal)
                else:
                    _LOGGER.warning(f"MQTT client is not available for {self.name}")

    async def dataRefresh(self, _update_count: int) -> None:
        """Refresh the device data."""
        from .api import Api

        if self.lastseen != datetime.min:
            self.mqttPublish(self.topic_read, {"properties": ["getAll"]}, self.mqtt)
        else:
            self.mqttPublish(self.topic_read, {"properties": ["getAll"]}, Api.mqttCloud)
            self.mqttPublish(self.topic_read, {"properties": ["getAll"]}, Api.mqttLocal)

    def mqttMessage(self, topic: str, payload: Any) -> bool:
        if topic == "register/replay":
            _LOGGER.info(f"Register replay for {self.name} => {payload}")
            return True

        return super().mqttMessage(topic, payload)


class ZendureZenSdk(ZendureDevice):
    """Zendure Zen SDK class for devices."""

    def __init__(self, hass: HomeAssistant, deviceId: str, name: str, model: str, definition: dict[str, str], parent: str | None = None) -> None:
        """Initialize Device."""
        self.session = async_get_clientsession(hass, verify_ssl=False)
        super().__init__(hass, deviceId, name, model, definition, parent)
        self.connection = ZendureRestoreSelect(self, "connection", {0: "cloud", 2: "zenSDK"}, self.mqttSelect, 0)
        self.httpid = 0

    async def mqttSelect(self, select: Any, _value: Any) -> None:
        from .api import Api

        self.mqtt = None
        match select.value:
            case 0:
                Api.mqttCloud.unsubscribe(f"/{self.prodkey}/{self.deviceId}/#")
                Api.mqttCloud.unsubscribe(f"iot/{self.prodkey}/{self.deviceId}/#")

            case 2:
                Api.mqttCloud.unsubscribe(f"/{self.prodkey}/{self.deviceId}/#")
                Api.mqttCloud.unsubscribe(f"iot/{self.prodkey}/{self.deviceId}/#")

        _LOGGER.debug(f"Mqtt selected {self.name}")

    async def entityWrite(self, entity: EntityZendure, value: Any) -> None:
        if entity.unique_id is None:
            _LOGGER.error(f"Entity {entity.name} has no unique_id, cannot write property {self.name}")
            return

        property_name = entity.unique_id[(len(self.name) + 1) :]
        _LOGGER.info(f"Writing property {self.name} {property_name} => {value}")

        await self.httpPost("properties/write", {"properties": {property_name: value}})

    async def power_get(self) -> int:
        """Get the current power."""
        if self.connection.value != 0:
            json = await self.httpGet("properties/report")
            self.mqttProperties(json)

        # return zero if device is offline or states are unknown
        if not self.online or self.outputHomePower.state is None:
            return 0
        self.powerAct = self.outputHomePower.asInt if self.gridInputPower.state is None else self.outputHomePower.asInt - self.gridInputPower.asInt 
        return self.powerAct

    def power_set(self, state: ManagerState, power: int) -> int:
        if len(self.ipAddress) == 0:
            _LOGGER.error(f"Cannot set power for {self.name} as IP address is not set")
            return power

        delta = abs(power - self.powerAct)
        if delta <= SmartMode.IGNORE_DELTA and state != ManagerState.IDLE:
            _LOGGER.info(f"Update power {self.name} => no action [power {power}]")
            return self.powerAct

        _LOGGER.info(f"Update power {self.name} => {power} state: {state} delta: {delta}")

        if power == 0:
            command = {"properties": {"smartMode": 0, "inputLimit": 0, "outputLimit": 0, "acMode": 1}}
        elif state == ManagerState.CHARGING:
            command = {"properties": {"smartMode": 1, "acMode": 1, "inputLimit": -power}}
        else:
            command = {"properties": {"smartMode": 1, "acMode": 2, "outputLimit": power}}

        if self.connection.value != 0:
            self.hass.async_create_task(self.httpPost("properties/write", command))
        else:
            self.mqttPublish(self.topic_write, command, self.mqtt)

        return power

    async def httpGet(self, url: str, key: str | None = None) -> dict[str, Any]:
        try:
            url = f"http://{self.ipAddress}/{url}"
            response = await self.session.get(url, headers=CONST_HEADER)
            self.lastseen = datetime.now()
            payload = json.loads(await response.text())
            return payload if key is None else payload.get(key, {})
        except Exception as e:
            _LOGGER.error(f"HttpGet error {self.name} {e}!")
        return {}

    async def httpPost(self, url: str, command: Any) -> None:
        try:
            self.httpid += 1
            command["id"] = self.httpid
            command["sn"] = self.snNumber
            url = f"http://{self.ipAddress}/{url}"
            await self.session.post(url, json=command, headers=CONST_HEADER)
        except Exception as e:
            _LOGGER.error(f"HttpPost error {self.name} {e}!")
