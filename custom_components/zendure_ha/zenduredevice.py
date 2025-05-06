"""Zendure Integration device."""

from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timedelta
from typing import Any

from bleak import BleakClient
from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.util import dt as dt_util
from paho.mqtt import client as mqtt_client

from .const import AcMode
from .select import ZendureSelect
from .sensor import ZendureRestoreSensor, ZendureSensor
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
    mqttCloud = ""
    mqttLocal: str | None = None
    mqttLog: bool = False
    wifissid: str | None = None
    wifipsw: str | None = None
    _messageid = 1000

    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any, parent: str | None = None) -> None:
        """Initialize ZendureDevice."""
        self.snNumber = definition["snNumber"]
        super().__init__(hass, definition["name"], prodName, self.snNumber, parent)
        self.deviceId = deviceId
        self.prodkey = definition["productKey"]
        self._topic_read = f"iot/{self.prodkey}/{self.deviceId}/properties/read"
        self._topic_write = f"iot/{self.prodkey}/{self.deviceId}/properties/write"
        self.topic_function = f"iot/{self.prodkey}/{self.deviceId}/function/invoke"
        self.mqtt: mqtt_client.Client | None = None
        self._cloud: mqtt_client.Client | None = None
        self.batteries: list[ZendureBattery] = []
        self.devices.append(self)

        self.lastUpdate = datetime.min
        self.service_info: bluetooth.BluetoothServiceInfoBleak | None = None

        self.powerMax = 0
        self.powerMin = 0
        self.powerAct = 0
        self.capacity = 0
        self.kwh = 0
        self.clusterType: Any = 0
        self.clusterdevices: list[ZendureDevice] = []
        self.powerSensors: list[ZendureSensor] = []

    def entitiesCreate(self) -> None:
        super().entitiesCreate()
        if len(self.devices) > 1:
            clusters: dict[Any, str] = {0: "clusterunknown", 1: "clusterowncircuit", 2: "cluster800", 3: "cluster1200", 4: "cluster2400"}
            for d in self.devices:
                if d != self:
                    clusters[d.deviceId] = f"Part of {d.name} cluster"

            ZendureSelect.addSelects([
                self.select(
                    "cluster",
                    clusters,
                    self.clusterUpdate,
                    True,
                )
            ])

        self.powerSensors = [
            self.sensor("aggrChargeDaykWh", None, "kWh", "energy", "total", 2, True),
            self.sensor("aggrDischargeDaykWh", None, "kWh", "energy", "total", 2, True),
            self.sensor("aggrChargeTotalkWh", None, "kWh", "energy", "total", 2, True),
            self.sensor("aggrDischargeTotalkWh", None, "kWh", "energy", "total", 2, True),
            self.sensor("aggrSolarTotalkWh", None, "kWh", "energy", "total", 2, True),
        ]
        ZendureSensor.addSensors(self.powerSensors)

        if self.mqttLocal:
            ZendureSwitch.addSwitches([
                self.switch("MqttLocal", None, "switch", self.mqttSwitchMqtt, False),
            ])

    def entityChanged(self, key: str, _entity: Entity, value: Any) -> None:
        match key:
            case "outputPackPower":
                self.powerAct = int(value)
                self.update_aggr([int(value), 0, int(value), 0, 0])
            case "packInputPower":
                self.powerAct = -int(value)
                self.update_aggr([0, int(value), 0, int(value), 0])
            case "solarInputPower":
                self.update_aggr([0, 0, 0, 0, int(value)])

    def entityWrite(self, entity: Entity, value: Any) -> None:
        _LOGGER.info(f"Writing property {self.name} {entity.name} => {value}")
        if entity.unique_id is None:
            _LOGGER.error(f"Entity {entity.name} has no unique_id.")
            return

        property_name = entity.unique_id[(len(self.name) + 1) :]
        if property_name in {"minSoc", "socSet"}:
            value = int(value * 10)

        self.writeProperties({property_name: value})

    def mqttSwitchMqtt(self, entity: ZendureSwitch, value: Any) -> None:
        if self.service_info is None or self.mqtt is None or self._cloud is None:
            _LOGGER.info(f"Unable to set mqtt {self.name} => no bluetooth device")
            return
        entity.update_value(value)
        self._hass.async_create_task(self.bleMqtt(value == 1))

    def mqttInit(self, mqtt: mqtt_client.Client) -> None:
        _LOGGER.info(f"Init mqtt: {self.name}")
        self.mqtt = mqtt
        if self.mqtt:
            _LOGGER.info(f"Subscribe mqtt: {self.name}")
            self.mqtt.subscribe(f"/{self.prodkey}/{self.deviceId}/#")
            self.mqtt.subscribe(f"iot/{self.prodkey}/{self.deviceId}/#")
        self.mqttRefresh()

    def mqttMessage(self, topics: list[str], payload: Any) -> None:
        try:
            parameter = topics[-1]

            match parameter:
                case "report":
                    self.lastUpdate = datetime.now() + timedelta(seconds=30)
                    if properties := payload.get("properties", None):
                        for key, value in properties.items():
                            self.entityUpdate(key, value)

                    # update the battery properties
                    if batprops := payload.get("packData", None):
                        for b in batprops:
                            sn = b.pop("sn")
                            if (bat := ZendureBattery.batterydict.get(sn, None)) is None:
                                match sn[0]:
                                    case "A":
                                        bat = ZendureBattery(self._hass, sn, "AB1000", sn, self.name, 1)
                                    case "C":
                                        bat = ZendureBattery(self._hass, sn, "AB2000" + ("S" if sn[3] == "F" else ""), sn, self.name, 2)
                                    case "F":
                                        bat = ZendureBattery(self._hass, sn, "AB3000", sn, self.name, 3)
                                    case _:
                                        bat = ZendureBattery(self._hass, sn, "AB????", sn, self.name, 3)
                                self.kwh += bat.kwh
                                self._hass.loop.call_soon_threadsafe(bat.entitiesCreate, self)

                            for key, value in b.items():
                                bat.entityUpdate(key, value)

                case "reply":
                    if self.mqttLog and topics[-3] == "function":
                        _LOGGER.info(f"Receive: {self.name} => ready!")
                    return

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())

    def mqttInvoke(self, command: Any) -> None:
        if self.mqtt:
            ZendureDevice._messageid += 1
            payload = json.dumps(command, default=lambda o: o.__dict__)
            if self.mqttLog:
                _LOGGER.info(f"Invoke function {self.name} => {payload}")
            self.mqtt.publish(self.topic_function, payload)

    def mqttRefresh(self) -> None:
        if self.mqtt:
            self.mqtt.publish(self._topic_read, '{"properties": ["getAll"]}')

    def mqttZendure(self, _mqttserver: str, psw: str) -> None:
        self._cloud = mqtt_client.Client(client_id=self.deviceId, clean_session=False, userdata=True)
        self._cloud.username_pw_set(username=self.deviceId, password=psw)
        self._cloud.on_connect = self.mqttZendureConnect
        self._cloud.on_message = self.mqttMessage
        # self._cloud.connect(mqttserver, 1883)
        # self._cloud.suppress_exceptions = True
        # self._cloud.loop_start()

    def mqttZendureConnect(self, _client: Any, _userdata: Any, _flags: Any, rc: Any) -> None:
        _LOGGER.info(f"Zendure Cloud Client has been connected, return code: {rc}")
        if self._cloud:
            self.mqttInit(self._cloud)

    def mqttZendureMessage(self, _client: Any, _userdata: Any, msg: Any) -> None:
        try:
            # check for valid device in payload
            topics = msg.topic.split("/")
            if topics[2] == self.deviceId:
                topics[2] = self.name
                payload = json.loads(msg.payload.decode())
                payload.pop("deviceId", None)
                if ZendureDevice.mqttLog:
                    _LOGGER.info(f"Zendure Topic: {self.name} {msg.topic.replace(self.deviceId, self.name)} => {payload}")
                self.mqttMessage(topics, payload)

        except:  # noqa: E722
            return

    def update_aggr(self, values: list[int]) -> None:
        try:
            time = dt_util.now()
            for i in range(len(values)):
                s = self.powerSensors[i]
                if isinstance(s, ZendureRestoreSensor):
                    s.aggregate(time, values[i], i<2)
        except Exception as err:
            _LOGGER.error(err)

    def update_ac_mode(self, _entity: ZendureSelect, mode: int) -> None:
        if mode == AcMode.INPUT:
            self.writeProperties({"acMode": mode, "inputLimit": self.asInt("inputLimit")})
        elif mode == AcMode.OUTPUT:
            self.writeProperties({"acMode": mode, "outputLimit": self.asInt("outputLimit")})

    def writeProperties(self, props: dict[str, Any]) -> None:
        if self.mqtt:
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
            self.mqtt.publish(self._topic_write, payload)

    def writePower(self, power: int, inprogram: bool) -> None:
        _LOGGER.info(f"Update power {self.name} => {power} capacity {self.capacity} [program {inprogram}]")

    async def bleMqtt(self, local: bool) -> None:
        if self.service_info is None:
            return
        # get the bluetooth device
        if self.service_info.connectable:
            device = self.service_info.device
        elif connectable_device := bluetooth.async_ble_device_from_address(self._hass, self.service_info.device.address, True):
            device = connectable_device
        else:
            return

        try:
            server = self.mqttLocal if local else self.mqttCloud
            _LOGGER.info(f"Set mqtt {self.name} to {server}")

            async with BleakClient(device) as client:
                await self.bleCommand(
                    client,
                    {
                        "iotUrl": server,
                        "messageId": str(self._messageid),
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
                        "messageId": str(self._messageid),
                        "method": "station",
                    },
                )

        except TimeoutError:
            _LOGGER.debug(f"Timeout when trying to connect to {self.name} {self.service_info.name}")
        except (AttributeError, BleakError) as err:
            _LOGGER.debug(f"Could not connect to {self.name}: {err}")
        except Exception as err:
            _LOGGER.error(f"BLE error: {err}")
            _LOGGER.error(traceback.format_exc())

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
