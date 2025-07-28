"""Zendure Integration device."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import traceback
from base64 import b64decode
from collections.abc import Callable
from datetime import datetime
from typing import Any, Mapping

from bleak import cli
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.providers import homeassistant as auth_ha
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from paho.mqtt import client as mqtt_client
from paho.mqtt import enums as mqtt_enums

from .const import (
    CONF_APPTOKEN,
    CONF_BETA,
    CONF_HAKEY,
    CONF_MQTTPORT,
    CONF_MQTTPSW,
    CONF_MQTTSERVER,
    CONF_MQTTUSER,
    CONF_WIFIPSW,
    CONF_WIFISSID,
    CONF_MQTTLOG,
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


class Api:
    """Zendure API class."""

    createdevice: dict[str, Callable[[HomeAssistant, str, str, Any], ZendureDevice]] = {
        "ace 1500": ACE1500,
        "aio 2400": AIO2400,
        "hub 1200": Hub1200,
        "hub 2000": Hub2000,
        "hyper 2000": Hyper2000,
        "solarflow 800": SolarFlow800,
        "solarflow 800 pro": SolarFlow800Pro,
        "solarflow 2400 ac": SolarFlow2400AC,
        "superbase v6400": SuperBaseV6400,
    }
    mqttCloud = mqtt_client.Client(userdata="cloud")
    mqttLocal = mqtt_client.Client(userdata="local")
    mqttClients: dict[str, mqtt_client.Client] = {}
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

    def __init__(self) -> None:
        """Initialize the API."""

    def Init(self, data: Mapping[str, Any], mqtt: Mapping[str, Any]) -> None:
        """Initialize Zendure Api."""
        self.mqttCloud.__init__(mqtt_enums.CallbackAPIVersion.VERSION2, mqtt["clientId"], False, "cloud")
        url = mqtt["url"]
        Api.cloudServer, Api.cloudPort = url.rsplit(":", 1) if ":" in url else (url, "1883")
        self.mqttInit(self.mqttCloud, Api.cloudServer, Api.cloudPort, mqtt["username"], mqtt["password"])
        Api.mqttLogging = data.get(CONF_MQTTLOG, False)

        # Get wifi settings
        Api.wifissid = data.get(CONF_WIFISSID, "")
        Api.wifipsw = data.get(CONF_WIFIPSW, "")

        # Get local Mqtt settings
        Api.localServer = data.get(CONF_MQTTSERVER, "")
        Api.localPort = data.get(CONF_MQTTPORT, 1883)
        Api.localUser = data.get(CONF_MQTTUSER, "")
        Api.localPassword = data.get(CONF_MQTTPSW, "")
        if Api.localServer != "":
            self.mqttLocal.__init__(mqtt_enums.CallbackAPIVersion.VERSION2, Api.localUser, False, "local")
            self.mqttInit(self.mqttLocal, Api.localServer, Api.localPort, Api.localUser, Api.localPassword)
            self.mqttLocal.subscribe("/#")
            self.mqttLocal.subscribe("iot/#")

    @staticmethod
    async def Connect(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any] | None:
        """Connect to the Zendure API."""
        if data.get(CONF_BETA, False):
            return await Api.ApiHA(hass, data)
        return await Api.ApiOld(hass, data)

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

    @staticmethod
    async def ApiOld(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any] | None:
        if (username := data.get(CONF_USERNAME, "")) == "" or (password := data.get(CONF_PASSWORD, "")) == "":
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_user_password")

        session = async_get_clientsession(hass)
        headers = {
            "Content-Type": "application/json",
            "Accept-Language": "en-EN",
            "appVersion": "4.3.1",
            "User-Agent": "Zendure/4.3.1 (iPhone; iOS 14.4.2; Scale/3.00)",
            "Accept": "*/*",
            "Blade-Auth": "bearer (null)",
        }
        authBody = {
            "password": password,
            "account": username,
            "appId": "121c83f761305d6cf7e",
            "appType": "iOS",
            "grantType": "password",
            "tenantId": "",
        }

        try:
            url = "https://app.zendure.tech/v2/auth/app/token"
            response = await session.post(url=url, json=authBody, headers=headers)

            if not response.ok:
                return None

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
                return None
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
                })

            # create devices
            result = {
                "mqtt": mqtt,
                "deviceList": devices,
            }

        except Exception as e:
            _LOGGER.error(f"Unable to connect to Zendure {zen_api} {e}!")
            return None
        else:
            return result

    def mqttInit(self, client: mqtt_client.Client, srv: str, port: str, user: str, psw: str) -> None:
        client.on_connect = self.mqttConnect
        client.on_message = self.mqttMsgCloud if client == self.mqttCloud else self.mqttMsgLocal
        client.suppress_exceptions = True
        client.username_pw_set(user, psw)
        client.connect(srv, int(port))
        client.loop_start()
        self.mqttClients[srv] = client

    def mqttServer(self, server: str, port: int, user: str, password: str) -> mqtt_client.Client:
        """Create a Zendure device."""
        if (mqtt := self.mqttClients.get(server if server != "" else self.cloudServer, None)) is None:
            mqtt = mqtt_client.Client(mqtt_enums.CallbackAPIVersion.VERSION2, user, userdata=server)
            self.mqttInit(mqtt, server, str(port), user, password)
            self.mqttClients[server] = mqtt
        return self.mqttCloud

    def mqttConnect(self, client: Any, userdata: Any, _flags: Any, rc: Any, _props: Any) -> None:
        _LOGGER.info(f"Client {userdata} connected to MQTT broker, return code: {rc}")
        for device in self.devices.values():
            if device.mqtt == client:
                client.subscribe(f"/{device.prodkey}/{device.deviceId}/#")
                client.subscribe(f"iot/{device.prodkey}/{device.deviceId}/#")

    def mqttMsgCloud(self, client: Any, _userdata: Any, msg: Any) -> None:
        if msg.payload is None or not msg.payload:
            return
        try:
            topics = msg.topic.split("/", 3)
            deviceId = topics[2]
            if (device := self.devices.get(deviceId, None)) is not None:
                # check for valid device in payload
                payload = json.loads(msg.payload.decode())
                payload.pop("deviceId", None)

                if "isHA" in payload:
                    return
                if topics[0] == "" and client != device.mqtt:
                    device.mqttSet(client)
                    if device.zendure is not None:
                        device.zendure.loop_stop()
                        device.zendure.disconnect()
                        device.zendure = None

                if self.mqttLogging:
                    _LOGGER.info(f"Topic: {msg.topic.replace(deviceId, device.name)} => {payload}")
                device.mqttMessage(topics[3], payload)
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
                payload = json.loads(msg.payload.decode())
                payload.pop("deviceId", None)

                if self.mqttLogging:
                    _LOGGER.info(f"Topic: {msg.topic.replace(deviceId, device.name)} => {payload}")

                device.mqttMessage(topics[3], payload)
                if topics[0] == "":
                    if client != device.mqtt:
                        device.mqttSet(client)

                    if device.zendure is None:
                        psw = asyncio.run_coroutine_threadsafe(self.mqttUser(device.hass, device.deviceId), device.hass.loop).result()
                        device.zendure = mqtt_client.Client(mqtt_enums.CallbackAPIVersion.VERSION2, device.deviceId, False, "zendure")
                        self.mqttInit(device.zendure, Api.cloudServer, Api.cloudPort, device.deviceId, psw)
                        device.zendure.on_message = self.mqttMsgDevice
                        device.mqtt = self.mqttLocal
                    payload["deviceId"] = device.deviceId
                    payload["isHA"] = True
                    device.zendure.publish(msg.topic, json.dumps(payload))
                    _LOGGER.info(f"Forwarding message from device {device.name} to cloud: {msg.topic} => {msg.payload}")
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
                _LOGGER.info(f"Relaying message from device {device.name} to cloud: {msg.topic} => {msg.payload}")

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())

    async def mqttUser(self, hass: HomeAssistant, username: str) -> str:
        """Ensure the user exists."""
        psw = hashlib.md5(username.encode()).hexdigest().upper()[8:24]  # noqa: S324
        try:
            provider: auth_ha.HassAuthProvider = auth_ha.async_get_provider(hass)
            credentials = await provider.async_get_or_create_credentials({"username": username.lower()})
            user = await hass.auth.async_get_user_by_credentials(credentials)
            if user is None:
                user = await hass.auth.async_create_user(username, group_ids=[GROUP_ID_USER], local_only=False)
                await provider.async_add_auth(username.lower(), psw)
                await hass.auth.async_link_user(user, credentials)
            else:
                await provider.async_change_password(username.lower(), psw)

            _LOGGER.info(f"Created MQTT user: {username} with password: {psw}")

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())
        return psw
