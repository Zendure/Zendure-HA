"""Zendure Integration device."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import traceback
from base64 import b64decode
from collections.abc import Callable
from datetime import datetime
from typing import Any, Mapping

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from paho.mqtt import client as mqtt_client
from paho.mqtt import enums as mqtt_enums

from .const import (
    CONF_APPTOKEN,
    CONF_HAKEY,
    CONF_MQTTLOG,
    CONF_MQTTPORT,
    CONF_MQTTPSW,
    CONF_MQTTSERVER,
    CONF_MQTTUSER,
    CONF_WIFIPSW,
    CONF_WIFISSID,
    DOMAIN,
)
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

ZENDURE_MANAGER_STORAGE_VERSION = 1
ZENDURE_DEVICES = "devices"


class Api:
    """Zendure API class."""

    createdevice: dict[str, Callable[[HomeAssistant, str, str, Any], ZendureDevice]] = {
        "ace 1500": ACE1500,
        "aio 2400": AIO2400,
        "solarflow aio zy": AIO2400,
        "hub 1200": Hub1200,
        "solarflow2.0": Hub1200,
        "hub 2000": Hub2000,
        "solarflow hub 2000": Hub2000,
        "hyper 2000": Hyper2000,
        "hyper2000_3.0": Hyper2000,
        "solarflow 800": SolarFlow800,
        "solarflow 800 pro": SolarFlow800Pro,
        "solarflow 2400 ac": SolarFlow2400AC,
        "superbase v6400": SuperBaseV6400,
    }
    mqttCloud = mqtt_client.Client(userdata="cloud")
    mqttLocal = mqtt_client.Client(userdata="local")
    mqttLogging: bool = False
    devices: dict[str, ZendureDevice] = {}
    cloudServer: str = ""
    cloudPort: str = ""
    localServer: str = ""
    localPort: str = ""
    localUser: str = ""
    localPassword: str = ""
    wifipsw: str = ""
    wifissid: str = ""

    def Init(self, data: Mapping[str, Any], mqtt: Mapping[str, Any]) -> None:
        """Initialize Zendure Api."""
        Api.mqttLogging = data.get(CONF_MQTTLOG, False)
        Api.mqttCloud.__init__(mqtt_enums.CallbackAPIVersion.VERSION2, mqtt["clientId"], False, "cloud")
        url = mqtt["url"]
        Api.cloudServer, Api.cloudPort = url.rsplit(":", 1) if ":" in url else (url, "1883")
        self.mqttInit(Api.mqttCloud, Api.cloudServer, Api.cloudPort, mqtt["username"], mqtt["password"])

        # Get wifi settings
        Api.wifissid = data.get(CONF_WIFISSID, "")
        Api.wifipsw = data.get(CONF_WIFIPSW, "")

        # Get local Mqtt settings
        Api.localServer = data.get(CONF_MQTTSERVER, "")
        Api.localPort = data.get(CONF_MQTTPORT, 1883)
        Api.localUser = data.get(CONF_MQTTUSER, "")
        Api.localPassword = data.get(CONF_MQTTPSW, "")
        if Api.localServer != "":
            clientId = Api.localUser + str(secrets.randbelow(10000))
            self.mqttLocal.__init__(mqtt_enums.CallbackAPIVersion.VERSION2, clientId, True, "local")
            self.mqttInit(self.mqttLocal, Api.localServer, Api.localPort, Api.localUser, Api.localPassword)

    @staticmethod
    async def Connect(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any] | None:
        """Connect to the Zendure API."""
        devices = await Api.ApiHA(hass, data)

        # Open the storage
        store = Store(hass, ZENDURE_MANAGER_STORAGE_VERSION, f"{DOMAIN}.storage")
        if devices is None or len(devices) == 0:
            # load configuration from storage
            if (storage := await store.async_load()) and isinstance(storage, dict):
                devices = storage.get(ZENDURE_DEVICES, {})
        else:
            # Save configuration to storage
            await store.async_save({ZENDURE_DEVICES: devices})

        return devices

    @staticmethod
    async def ApiHA(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any] | None:
        session = async_get_clientsession(hass)

        if (token := data.get(CONF_APPTOKEN)) is not None and len(token) > 1:
            base64_url = b64decode(str(token)).decode("utf-8")
            api_url, appKey = base64_url.rsplit(".", 1)
        else:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_zendure_token")

        try:
            body = {
                "appKey": appKey,
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

            result = await session.post(url=f"{api_url}/api/ha/deviceList", json=body, headers=headers)
            data = await result.json()
            if not data.get("success", False) or (json := data["data"]) is None:
                return None
            return dict(json)

        except Exception as e:
            _LOGGER.error(f"Unable to connect to Zendure {e}!")
            _LOGGER.error(traceback.format_exc())
            return None

    def mqttInit(self, client: mqtt_client.Client, srv: str, port: str, user: str, psw: str) -> None:
        client.on_connect = self.mqttConnect
        client.on_disconnect = self.mqttDisconnect
        client.on_message = self.mqttMsgCloud if client == self.mqttCloud else self.mqttMsgLocal if client == self.mqttLocal else self.mqttMsgDevice
        client.suppress_exceptions = True
        client.username_pw_set(user, psw)
        client.connect(srv, int(port))
        client.loop_start()

    def mqttConnect(self, client: Any, userdata: Any, _flags: Any, rc: Any, _props: Any) -> None:
        _LOGGER.info(f"Client {userdata} connected to MQTT broker, return code: {rc}")
        if userdata == "zendure":
            for device in self.devices.values():
                if client == device.zendure:
                    client.subscribe(f"iot/{device.prodkey}/{device.deviceId}/#")
                    Api.mqttCloud.unsubscribe(f"/{device.prodkey}/{device.deviceId}/#")
                    Api.mqttCloud.unsubscribe(f"iot/{device.prodkey}/{device.deviceId}/#")
        else:
            for device in self.devices.values():
                client.subscribe(f"/{device.prodkey}/{device.deviceId}/#")
                client.subscribe(f"iot/{device.prodkey}/{device.deviceId}/#")

    def mqttDisconnect(self, _client: Any, userdata: Any, _flags: Any, rc: Any, _props: Any) -> None:
        _LOGGER.info(f"Client {userdata} disconnected to MQTT broker, return code: {rc}")

    def mqttMsgCloud(self, client: Any, _userdata: Any, msg: Any) -> None:
        if msg.payload is None or not msg.payload:
            return
        try:
            topics = msg.topic.split("/", 3)
            deviceId = topics[2]

            if (device := self.devices.get(deviceId, None)) is not None:
                payload = json.loads(msg.payload.decode())
                payload.pop("deviceId", None)

                if "isHA" in payload:
                    return

                if self.mqttLogging:
                    _LOGGER.info(f"Topic: {msg.topic.replace(deviceId, device.name)} => {payload}")

                if device.mqttMessage(topics[3], payload) and device.mqtt != client:
                    device.mqtt = client

            else:
                _LOGGER.info(f"Unknown device: {deviceId} => {msg.topic} => {msg.payload}")

        except:  # noqa: E722
            return

    def mqttMsgLocal(self, client: Any, _userdata: Any, msg: Any) -> None:
        if msg.payload is None or not msg.payload or len(self.devices) == 0:
            return
        try:
            topics = msg.topic.split("/", 3)
            deviceId = topics[2]

            if (device := self.devices.get(deviceId, None)) is not None:
                try:
                    payload = json.loads(msg.payload.decode())
                except:
                    temp = msg.payload.decode()
                    temp = temp.replace("'", '"')
                    payload = json.loads(temp)

                payload.pop("deviceId", None)

                if "isHA" in payload:
                    return

                if self.mqttLogging:
                    _LOGGER.info(f"Topic: {msg.topic.replace(deviceId, device.name)} => {payload}")

                if device.mqttMessage(topics[3], payload):
                    if device.mqtt != client:
                        device.mqtt = client

                    if device.zendure is None:
                        psw = hashlib.md5(device.deviceId.encode()).hexdigest().upper()[8:24]  # noqa: S324
                        device.zendure = mqtt_client.Client(mqtt_enums.CallbackAPIVersion.VERSION2, device.deviceId, False, "zendure")
                        self.mqttInit(device.zendure, Api.cloudServer, Api.cloudPort, device.deviceId, psw)

                    if device.zendure is not None and device.zendure.is_connected():
                        payload["deviceId"] = device.deviceId
                        payload["isHA"] = True
                        device.zendure.publish(msg.topic, json.dumps(payload))
                    if self.mqttLogging:
                        _LOGGER.info(f"Forwarding message from device {device.name} to cloud: {msg.topic} => {payload}")
            else:
                _LOGGER.info(f"Local message from device {msg.topic} => {msg.payload}")

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())

    def mqttMsgDevice(self, _client: Any, _userdata: Any, msg: Any) -> None:
        if msg.payload is None or not msg.payload:
            return
        try:
            topics = msg.topic.split("/", 3)
            deviceId = topics[2]

            if (device := self.devices.get(deviceId, None)) is not None and topics[0] == "iot":
                self.mqttLocal.publish(msg.topic, msg.payload)
                if self.mqttLogging:
                    _LOGGER.info(f"Relaying message from device {device.name} to cloud: {msg.topic} => {msg.payload}")

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())
