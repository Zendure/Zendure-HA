"""Module for Zendure API integration with Home Assistant."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import traceback
from base64 import b64decode
from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from paho.mqtt import client as mqtt_client
from paho.mqtt import enums as mqtt_enums

from .const import CONF_APPTOKEN, CONF_BETA, CONF_HAKEY, CONF_MQTTLOG
from .device import ZendureDevice
from .devices.ace1500 import ACE1500
from .devices.aio2400 import AIO2400
from .devices.hub1200 import Hub1200
from .devices.hub2000 import Hub2000
from .devices.hyper2000 import Hyper2000
from .devices.solarflow800 import SolarFlow800
from .devices.solarflow800Pro import SolarFlow800Pro
from .devices.solarflow2400ac import SolarFlow2400AC
from .devices.superbasev6400 import SuperBaseV6400

_LOGGER = logging.getLogger(__name__)


class Api:
    """Class for Zendure API."""

    def __init__(self, hass: HomeAssistant, data: dict[str, Any]) -> None:
        """Initialize the API."""
        self.hass = hass
        if data.get(CONF_BETA, False) and (token := data.get(CONF_APPTOKEN)) is not None and len(token) > 1:
            base64_url = b64decode(str(token)).decode("utf-8")
            self.api_url, self.appKey = base64_url.rsplit(".", 1)
        self.mqttLog = data.get(CONF_MQTTLOG, False)
        self.mqttCloud = mqtt_client.Client(userdata="cloud")
        self.devices: dict[str, ZendureDevice] = {}
        self.snDevices: dict[str, ZendureDevice] = {}
        self.clients: dict[str, mqtt_client.Client] = {}

    async def load(self, connect: bool) -> bool:
        session = async_get_clientsession(self.hass)
        try:
            body = {
                "appKey": self.appKey,
            }

            # Prepare signature parameters
            timestamp = int(datetime.now().timestamp())
            nonce = str(secrets.randbelow(90000) + 10000)

            # Merge all parameters to be signed and sort by key in ascending order
            sign_params = {
                **body,
                "timestamp": timestamp,
                "nonce": nonce,
            }

            # Construct signature string
            body_str = "".join(f"{k}{v}" for k, v in sorted(sign_params.items()))

            # Calculate signature
            sign_str = f"{CONF_HAKEY}{body_str}{CONF_HAKEY}"
            sha1 = hashlib.sha1()  # noqa: S324
            sha1.update(sign_str.encode("utf-8"))
            sign = sha1.hexdigest().upper()

            # Build request headers
            headers = {
                "Content-Type": "application/json",
                "timestamp": str(timestamp),
                "nonce": nonce,
                "clientid": "zenHa",
                "sign": sign,
            }

            result = await session.post(url=f"{self.api_url}/api/ha/deviceList", json=body, headers=headers)
            data = await result.json()
            if not data.get("success", False) or (json := data["data"]) is None:
                return False

            if connect:
                await self.createdevices(json["mqtt"], json["deviceList"])

        except Exception as e:
            _LOGGER.error(f"Unable to connect to Zendure {e}!")
            _LOGGER.error(traceback.format_exc())
            return False
        return True

    async def unload(self) -> None:
        """Unload the manager."""
        for device in self.devices.values():
            await device.unload()

        self.devices.clear()
        self.snDevices.clear()
        for client in self.clients.values():
            if client.is_connected():
                client.loop_stop()
                client.disconnect()

    async def createdevices(self, mqtt: Any, devices: Any) -> None:
        # get the device creation functions
        createdevice: dict[str, Callable[[HomeAssistant, str, str, Any], ZendureDevice]] = {}
        createdevice["ace 1500"] = ACE1500
        createdevice["aio 2400"] = AIO2400
        createdevice["hub 1200"] = Hub1200
        createdevice["hub 2000"] = Hub2000
        createdevice["hyper2000_3.0"] = Hyper2000
        createdevice["hyper 2000"] = Hyper2000
        createdevice["solarflow 800"] = SolarFlow800
        createdevice["solarflow 800 pro"] = SolarFlow800Pro
        createdevice["solarflow 2400 ac"] = SolarFlow2400AC
        createdevice["superbase v6400"] = SuperBaseV6400

        # Connect to Cloud MQTT
        self.mqttCloud.__init__(mqtt_enums.CallbackAPIVersion.VERSION2, mqtt["clientId"], False, "cloud")
        url = mqtt["url"]
        if ":" in url:
            srv, port = url.rsplit(":", 1)
        else:
            srv, port = url, "1883"
        self.mqttInit(self.mqttCloud, srv, port, mqtt["username"], mqtt["password"])
        self.clients[""] = self.mqttCloud

        # load devices
        for dev in devices:
            try:
                if (deviceId := dev["deviceKey"]) is None or (prodModel := dev["productModel"]) is None:
                    continue
                _LOGGER.info(f"Adding device: {deviceId} {prodModel}")
                _LOGGER.info(f"Data: {dev}")

                init = createdevice.get(prodModel.lower(), None)
                if init is None:
                    _LOGGER.info(f"Device {prodModel} is not supported!")
                    continue

                device = init(self.hass, deviceId, prodModel, dev)
                self.devices[deviceId] = device
                self.snDevices[device.snNumber] = device

                # get the mqtt client for the device
                srv = dev.get("server", "")
                if (mqtt := self.clients.get(srv, None)) is None:
                    mqtt = mqtt_client.Client(mqtt_enums.CallbackAPIVersion.VERSION2, dev["username"], userdata=srv)
                    self.mqttInit(mqtt, srv, dev.get("port", 1883), dev["username"], dev["password"])
                device.mqtt = mqtt

            except Exception as e:
                _LOGGER.error(f"Unable to create device {e}!")

        # initialize the devices
        for device in self.devices.values():
            if device.mqtt.is_connected():
                device.mqtt.publish(f"iot/{device.prodkey}/{device.deviceId}/register/replay", None, 1, True)
                device.mqtt.subscribe(f"/{device.prodkey}/{device.deviceId}/#")
                device.mqtt.subscribe(f"iot/{device.prodkey}/{device.deviceId}/#")
                device.mqtt.subscribe("Zendure/#")

            self.mqttCloud.publish(f"iot/{device.prodkey}/{device.deviceId}/register/replay", None, 1, True)
            self.mqttCloud.subscribe(f"/{device.prodkey}/{device.deviceId}/#")
            self.mqttCloud.subscribe(f"iot/{device.prodkey}/{device.deviceId}/#")
            await device.mqttRefresh()

    def mqttInit(self, client: mqtt_client.Client, srv: str, port: str, user: str, psw: str) -> None:
        client.username_pw_set(user, psw)
        client.connect(srv, int(port))
        client.on_connect = self.mqttConnect
        client.on_disconnect = self.mqttDisconnect
        client.on_message = self.mqttMsgCloud if client == self.mqttCloud else self.mqttMsgHA
        client.suppress_exceptions = True
        client.loop_start()
        self.clients[srv] = client

    def mqttConnect(self, client: Any, userdata: Any, _flags: Any, rc: Any) -> None:
        _LOGGER.error(f"Client {userdata} connected to MQTT broker, return code: {rc}")
        for device in self.devices.values():
            client.subscribe(f"/{device.prodkey}/{device.deviceId}/#")
            client.subscribe(f"iot/{device.prodkey}/{device.deviceId}/#")
        client.subscribe("Zendure/#")

    def mqttDisconnect(self, _client: Any, userdata: Any, rc: Any, _props: Any) -> None:
        _LOGGER.info(f"Client {userdata} disconnected from MQTT broker with return code {rc}")

    def mqttMsgCloud(self, _client: Any, _userdata: Any, msg: Any) -> None:
        if msg.payload is None or not msg.payload:
            return
        try:
            topics = msg.topic.split("/", 3)
            deviceId = topics[2]
            if (device := self.devices.get(deviceId, None)) is not None:
                # check for valid device in payload
                payload = json.loads(msg.payload.decode())
                payload.pop("deviceId", None)

                # Ignore if message is relayed from Home Assistant
                if payload.get("isHA", False):
                    return

                if self.mqttLog:
                    _LOGGER.info(f"Topic: {msg.topic.replace(deviceId, device.name)} => {payload}")

                # if not device.isLocal:
                device.mqttMessage(topics[3], payload)
                # elif topics[0] == "iot":
                #     payload["isHA"] = True
                #     # device.mqttZenApp = datetime.now() + timedelta(seconds=60)
                #     self.mqttLocal.publish(msg.topic, payload)
            # device.mqttZendure += 1
            # if device.mqttZendure == 1:
            #     device.mqttStatus()
            else:
                _LOGGER.info(f"Unknown device: {deviceId} => {msg.topic} => {msg.payload}")

        except:  # noqa: E722
            return

    def mqttMsgHA(self, _client: Any, _userdata: Any, msg: Any) -> None:
        if msg.payload is None or not msg.payload:
            return
        try:
            topics = msg.topic.split("/", 3)
            deviceId = topics[2]
            # if topics[0] != "iot" and (device := self.snDevices.get(deviceId, None)) is not None and not topics[3].endswith("/availability"):
            #     value = msg.payload.decode()

            #     match topics[3]:
            #         case "packState":
            #             value = ["sleeping", "charging", "discharging"].index(value)
            #         case "socStatus":
            #             value = ["idle", "charging", "discharging"].index(value)

            #     device.entityUpdate(topics[3], value)
        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())


class ApiOld(Api):
    """Old API class for Zendure integration."""

    def __init__(self, hass: HomeAssistant, data: dict[str, Any]) -> None:
        """Initialize the API."""
        super().__init__(hass, data)
        self.username = data.get(CONF_USERNAME, "")
        self.password = data.get(CONF_PASSWORD, "")

    async def load(self, connect: bool) -> bool:
        _LOGGER.info("Connecting to Zendure")
        session = async_get_clientsession(self.hass)
        headers = {
            "Content-Type": "application/json",
            "Accept-Language": "en-EN",
            "appVersion": "4.3.1",
            "User-Agent": "Zendure/4.3.1 (iPhone; iOS 14.4.2; Scale/3.00)",
            "Accept": "*/*",
            "Blade-Auth": "bearer (null)",
        }
        authBody = {
            "password": self.password,
            "account": self.username,
            "appId": "121c83f761305d6cf7e",
            "appType": "iOS",
            "grantType": "password",
            "tenantId": "",
        }

        try:
            url = "https://app.zendure.tech/v2/auth/app/token"
            response = await session.post(url=url, json=authBody, headers=headers)

            if not response.ok:
                return False
            if not connect:
                return True

            respJson = await response.json()
            json = respJson["data"]
            zen_api = json["serverNodeUrl"]
            mqttUrl = json["iotUrl"]
            if zen_api.endswith("eu"):
                mqttinfo = "SDZzJGo5Q3ROYTBO"
            else:
                zen_api = "https://app.zendure.tech/v2"
                mqttinfo = "b0sjUENneTZPWnhk"

            token = json["accessToken"]
            mqtt = {
                "clientId": token,
                "username": "zenApp",
                "password": b64decode(mqttinfo.encode()).decode("latin-1"),
                "url": mqttUrl + ":1883",
            }

            headers["Blade-Auth"] = f"bearer {token}"
            _LOGGER.info(f"Connected to {zen_api} => Mqtt: {mqttUrl}")

            url = f"{zen_api}/productModule/device/queryDeviceListByConsumerId"
            response = await session.post(url=url, headers=headers)
            if not response.ok:
                return False
            respJson = await response.json()
            json = respJson["data"]

            devices = list[Any]()
            for device in json:
                devices.append({
                    "deviceName": device["name"],
                    "productModel": device["productName"],
                    "productKey": device["productKey"],
                    "snNumber": device["snNumber"],
                    "deviceKey": device["deviceKey"],
                    "isLegacy": True,
                })

            # create devices
            await self.createdevices(mqtt, devices)

        except Exception as e:
            _LOGGER.error(f"Unable to connect to Zendure {zen_api} {e}!")
            return False

        return True
