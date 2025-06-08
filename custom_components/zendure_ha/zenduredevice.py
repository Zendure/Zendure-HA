"""Zendure Integration device."""

from __future__ import annotations

import json
import logging
import threading
import traceback
from datetime import datetime
from typing import Any

from bleak import BleakClient
from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from paho.mqtt import client as mqtt_client
from paho.mqtt import enums as mqtt_enums

from .const import AcMode, MqttState
from .select import ZendureSelect
from .sensor import ZendureSensor
from .switch import ZendureSwitch
from .zendurebase import ZendureBase
from .zendurebattery import ZendureBattery

_LOGGER = logging.getLogger(__name__)

SF_COMMAND_CHAR = "0000c304-0000-1000-8000-00805f9b34fb"


class ZendureDevice(ZendureBase):
    """A Zendure Device."""

    devicedict: dict[str, ZendureDevice] = {}
    devices: list[ZendureDevice] = []
    clusters: list[ZendureDevice] = []
    mqttClient = mqtt_client.Client()
    mqttCloud = mqtt_client.Client()
    mqttCloudUrl = ""
    mqttIsLocal: bool = False
    mqttLocalUrl = ""
    mqttLog: bool = False
    wifissid: str | None = None
    wifipsw: str | None = None
    _messageid = 700000

    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any, parent: str | None = None) -> None:
        """Initialize ZendureDevice."""
        self.deviceId = deviceId
        self.snNumber = definition["snNumber"]
        self.prodkey = definition["productKey"]
        super().__init__(hass, definition["name"], prodName, self.snNumber, parent)
        self._topic_read = f"iot/{self.prodkey}/{self.deviceId}/properties/read"
        self._topic_write = f"iot/{self.prodkey}/{self.deviceId}/properties/write"
        self.topic_function = f"iot/{self.prodkey}/{self.deviceId}/function/invoke"

        self.devicedict[deviceId] = self
        self.devices.append(self)

        self.mqttDevice = self.mqttClient
        self.mqttLocal = 0
        self.mqttZendure = 0
        self.mqttZenApp = datetime.min
        self.bleInfo: bluetooth.BluetoothServiceInfoBleak | None = None
        self.bleErr = False

        self.powerMax = 0
        self.powerMin = 0
        self.powerAct = 0
        self.capacity = 0.0
        self.clusterType: Any = 0
        self.clusterdevices: list[ZendureDevice] = []

    def entitiesCreate(self) -> None:
        super().entitiesCreate()
        if len(self.devices) > 1:
            clusters: dict[Any, str] = {0: "clusterunknown", 1: "clusterowncircuit", 2: "cluster800", 3: "cluster1200", 4: "cluster2400", 5: "cluster3600"}
            for d in self.devices:
                if d != self:
                    clusters[d.deviceId] = f"Part of {d.name} cluster"

            ZendureSelect.add([self.select("cluster", clusters, self.clusterUpdate, True)])

        ZendureSensor.add([
            self.sensor("aggrChargeTotal", None, "kWh", "energy", "total_increasing", 2, True),
            self.sensor("aggrDischargeTotal", None, "kWh", "energy", "total_increasing", 2, True),
            self.sensor("aggrSolarTotal", None, "kWh", "energy", "total_increasing", 2, True),
            self.sensor("ConnectionStatus"),
        ])

        def doMqttReset(entity: ZendureSwitch, value: Any) -> None:
            entity.update_value(value)
            self._hass.async_create_task(self.bleMqtt())

        ZendureSwitch.add([self.switch("MqttReset", onwrite=doMqttReset, value=False)])

    def entitiesBattery(self, _battery: ZendureBattery, _sensors: list[ZendureSensor]) -> None:
        return

    def entityChanged(self, key: str, _entity: Entity, value: Any) -> None:
        match key:
            case "outputPackPower":
                self.powerAct = int(value)
                self.aggr("aggrChargeTotal", int(value))
                self.aggr("aggrDischargeTotal", 0)
            case "packInputPower":
                self.aggr("aggrChargeTotal", 0)
                self.aggr("aggrDischargeTotal", int(value))
            case "solarInputPower":
                self.aggr("aggrSolarTotal", int(value))

    def entityWrite(self, entity: Entity, value: Any) -> None:
        _LOGGER.info(f"Writing property {self.name} {entity.name} => {value}")
        if entity.unique_id is None:
            _LOGGER.error(f"Entity {entity.name} has no unique_id.")
            return

        property_name = entity.unique_id[(len(self.name) + 1) :]
        if property_name in {"minSoc", "socSet"}:
            value = int(value * 10)

        self.writeProperties({property_name: value})

    def deviceMqttClient(self, mqttPsw: str) -> None:
        """Initialize MQTT client for device."""
        self.mqttDevice = mqtt_client.Client(mqtt_enums.CallbackAPIVersion.VERSION1, client_id=self.deviceId, clean_session=False)
        self.mqttDevice.username_pw_set(username=self.deviceId, password=mqttPsw)
        self.mqttDevice.on_connect = self.deviceConnect
        self.mqttDevice.on_disconnect = self.deviceDisconnect
        self.mqttDevice.on_message = self.deviceMessage
        self.mqttDevice.suppress_exceptions = True

    def deviceConnect(self, _client: mqtt_client.Client, _userdata: Any, _flags: Any, _rc: Any) -> None:
        """Handle MQTT connection for device."""
        self.mqttStatus()

    def deviceDisconnect(self, _client: Any, _userdata: Any, _rc: Any) -> None:
        """Handle MQTT disconnection for device."""
        self.mqttStatus()

    def deviceMessage(self, _client: Any, _userdata: Any, msg: Any) -> None:
        """Handle MQTT message for device."""
        _LOGGER.info(f"Device {self.name} received message: {msg.topic} {msg.payload}")

    def mqttInvoke(self, command: Any) -> None:
        self._messageid += 1
        command["messageId"] = self._messageid
        command["deviceKey"] = self.deviceId
        command["timestamp"] = int(datetime.now().timestamp())
        payload = json.dumps(command, default=lambda o: o.__dict__)
        if self.mqttLog:
            _LOGGER.info(f"Invoke function {self.name} => {payload}")
        self.mqttClient.publish(self.topic_function, payload)

    def mqttMessage(self, topics: list[str], payload: Any) -> bool:
        try:
            parameter = topics[-1]
            match parameter:
                case "register":
                    _LOGGER.info(f"Register {self.name} => {payload}")
                    self.mqttRefresh(False)

                case "report":
                    if properties := payload.get("properties", None):
                        for key, value in properties.items():
                            self.entityUpdate(key, value)

                    # update the battery properties
                    if batprops := payload.get("packData", None):
                        for b in batprops:
                            sn = b.pop("sn")

                            if ((bat := ZendureBattery.batterydict.get(sn, None)) is None) and (not b):
                                match sn[0]:
                                    case "A":
                                        if sn[3] == "3":
                                            bat = ZendureBattery(self._hass, sn, "AIO2400", sn, self.name, 2.4)
                                        else:
                                            bat = ZendureBattery(self._hass, sn, "AB1000", sn, self.name, 0.96)
                                    case "B":
                                        bat = ZendureBattery(self._hass, sn, "AB1000S", sn, self.name, 0.96)
                                    case "C":
                                        bat = ZendureBattery(self._hass, sn, "AB2000" + ("S" if sn[3] == "F" else ""), sn, self.name, 1.92)
                                    case "F":
                                        bat = ZendureBattery(self._hass, sn, "AB3000", sn, self.name, 2.88)
                                    case _:
                                        bat = ZendureBattery(self._hass, sn, "AB????", sn, self.name, 2.88)
                                self.kwh += bat.kwh
                                done = threading.Event()
                                self._hass.loop.call_soon_threadsafe(bat.entitiesCreate, self.entitiesBattery, done)
                                done.wait(10)

                            if (bat is not None) and bat.entities:
                                for key, value in b.items():
                                    bat.entityUpdate(key, value)
                    return True

                case "reply":
                    if self.mqttLog and topics[-3] == "function":
                        _LOGGER.info(f"Receive: {self.name} => ready!")
                    return True

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())

        return False

    def mqttRefresh(self, reset: bool) -> None:
        self.mqttClient.publish(self._topic_read, '{"properties": ["getAll"]}')
        self.mqttStatus()
        if reset:
            self.mqttZendure = 0
            self.mqttLocal = 0

    def mqttStatus(self) -> None:
        status = MqttState.UNKNOWN

        if self.bleErr:
            status |= MqttState.BLE_ERR
        elif self.bleInfo is not None and self.bleInfo.connectable:
            status |= MqttState.BLE

        if self.mqttDevice is not None and self.mqttDevice.is_connected():
            status |= MqttState.APP
        elif self.mqttZendure > 0:
            status |= MqttState.CLOUD
        if self.mqttLocal > 0:
            status |= MqttState.LOCAL
            if self.mqttIsLocal and self.mqttDevice.host == "":
                self.mqttDevice.connect(self.mqttCloudUrl, 1883)
                self.mqttDevice.loop_start()

        self.entities["ConnectionStatus"].update_value(int(status.value))

    def update_ac_mode(self, _entity: ZendureSelect, mode: int) -> None:
        if mode == AcMode.INPUT:
            self.writeProperties({"acMode": mode, "inputLimit": self.asInt("inputLimit")})
        elif mode == AcMode.OUTPUT:
            self.writeProperties({"acMode": mode, "outputLimit": self.asInt("outputLimit")})

    def writeProperties(self, props: dict[str, Any]) -> None:
        ZendureDevice._messageid += 1
        payload = json.dumps(
            {
                "deviceId": self.deviceId,
                "messageId": ZendureDevice._messageid,
                "timestamp": int(datetime.now().timestamp()),
                "properties": props,
            },
            default=lambda o: o.__dict__,
        )
        self.mqttClient.publish(self._topic_write, payload)

    def writePower(self, power: int, inprogram: bool) -> None:
        _LOGGER.info(f"Update power {self.name} => {power} capacity {self.capacity} [program {inprogram}]")

    async def bleMqtt(self, server: str | None = None) -> None:
        """Set the MQTT server for the device via BLE."""
        try:
            self.bleErr = False
            if self.bleInfo is None:
                return
            if server is None:
                server = self.mqttLocalUrl if self.mqttIsLocal else self.mqttCloudUrl

            # get the bluetooth device
            if self.bleInfo.connectable:
                device = self.bleInfo.device
            elif connectable_device := bluetooth.async_ble_device_from_address(self._hass, self.bleInfo.device.address, True):
                device = connectable_device
            else:
                return

            try:
                _LOGGER.info(f"Set mqtt {self.name} to {server}")
                async with BleakClient(device) as client:
                    try:
                        await self.bleCommand(
                            client,
                            {
                                "iotUrl": server,
                                "messageId": 1002,
                                "method": "token",
                                "password": self.wifipsw,
                                "ssid": self.wifissid,
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
                _LOGGER.error(f"Timeout when trying to connect to {self.name} {self.bleInfo.name}")
                self.bleErr = True
            except (AttributeError, BleakError) as err:
                _LOGGER.error(f"Could not connect to {self.name}: {err}")
                self.bleErr = True
            except Exception as err:
                _LOGGER.error(f"BLE error: {err}")
                _LOGGER.error(traceback.format_exc())
                self.bleErr = True

        finally:
            self.setvalue("MqttReset", False)
            self.mqttStatus()

    async def bleCommand(self, client: BleakClient, command: Any) -> None:
        try:
            self._messageid += 1
            payload = json.dumps(command, default=lambda o: o.__dict__)
            b = bytearray()
            b.extend(map(ord, payload))
            _LOGGER.info(f"BLE command: {self.name} => {payload}")
            await client.write_gatt_char(SF_COMMAND_CHAR, b, response=False)
        except Exception as err:
            _LOGGER.error(f"BLE error: {err}")

    def clusterUpdate(self, _entity: ZendureSelect, cluster: Any) -> None:
        try:
            _LOGGER.info(f"Update cluster: {self.name} => {cluster}")
            self.clusterType = cluster

            for d in self.devices:
                if self in d.clusterdevices:
                    if d.deviceId != cluster:
                        _LOGGER.info(f"Remove {self.name} from cluster {d.name}")
                        if self in d.clusterdevices:
                            d.clusterdevices.remove(self)
                elif d.deviceId == cluster:
                    _LOGGER.info(f"Add {self.name} to cluster {d.name}")
                    if self not in d.clusterdevices:
                        d.clusterdevices.append(self)

            if cluster in [1, 2, 3, 4] and self not in self.clusters:
                self.clusters.append(self)
                if self not in self.clusterdevices:
                    self.clusterdevices.append(self)

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())

    @property
    def clustercapacity(self) -> int:
        """Get the capacity of the cluster."""
        if self.clusterType == 0:
            return 0
        return sum(d.capacity for d in self.clusterdevices)

    @property
    def clusterMax(self) -> int:
        """Get the maximum power of the cluster."""
        cmax = sum(d.powerMax for d in self.clusterdevices)
        match self.clusterType:
            case 1:
                cmax = min(cmax, 3600)
            case 2:
                cmax = min(cmax, 800)
            case 3:
                cmax = min(cmax, 1200)
            case 4:
                cmax = min(cmax, 2400)
            case 4:
                cmax = min(cmax, 3600)
            case _:
                return 0
        return cmax

    @property
    def clusterMin(self) -> int:
        """Get the minimum power of the cluster."""
        cmin = sum(d.powerMin for d in self.clusterdevices)
        match self.clusterType:
            case 1:
                cmin = min(cmin, -3600)
            case 2:
                cmin = min(cmin, -2400)
            case 3:
                cmin = min(cmin, -2400)
            case 4:
                cmin = min(cmin, -3600)
            case _:
                return 0
        return cmin
