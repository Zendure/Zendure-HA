"""Module for Zendure API integration with Home Assistant."""

from __future__ import annotations

from ipaddress import ip_address
import logging
from math import prod
import traceback
from base64 import b64decode
from collections.abc import Callable
from typing import Any

from aiohttp import ClientSession
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from paho.mqtt import client as mqtt_client

from .zenduredevice import ZendureDeviceDefinition

_LOGGER = logging.getLogger(__name__)

SF_AUTH_PATH = "/auth/app/token"
SF_DEVICELIST_PATH = "/productModule/device/queryDeviceListByConsumerId"
SF_DEVICEDETAILS_PATH = "/device/solarFlow/detail"


class Api:
    """Class for Zendure API."""

    def __init__(self, hass: HomeAssistant, data: dict[str, Any]) -> None:
        """Initialize the API."""
        self.hass = hass
        self.username = data[CONF_USERNAME]
        self.password = data[CONF_PASSWORD]
        self.session: ClientSession | None
        self.token: str = ""
        self.mqttUrl = ""
        self.zen_api = ""
        self.mqttinfo = ""

    async def connect(self) -> bool:
        _LOGGER.info("Connecting to Zendure")
        self.session = async_get_clientsession(self.hass)
        self.headers = {
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
            url = f"https://app.zendure.tech/v2{SF_AUTH_PATH}"
            response = await self.session.post(url=url, json=authBody, headers=self.headers)

            if response.ok:
                respJson = await response.json()
                json = respJson["data"]
                self.zen_api = json["serverNodeUrl"]
                self.mqttUrl = json["iotUrl"]
                if self.zen_api.endswith("eu"):
                    self.mqttinfo = "SDZzJGo5Q3ROYTBO"
                else:
                    self.zen_api = "https://app.zendure.tech/v2"
                    self.mqttinfo = "b0sjUENneTZPWnhk"

                self.token = json["accessToken"]
                self.headers["Blade-Auth"] = f"bearer {self.token}"
                _LOGGER.info(f"Connected to {self.zen_api} => Mqtt: {self.mqttUrl}")
                return True

        except Exception as e:
            _LOGGER.error(f"Unable to connect to Zendure {self.zen_api} {e}!")
            return False

        _LOGGER.error(f"Unable to connect to Zendure {self.zen_api}!")
        return False

    def disconnect(self) -> None:
        self.session.close()
        self.session = None

    def get_mqtt(self, onMessage: Callable) -> mqtt_client.Client:
        _LOGGER.info(f"Creating mqtt client {self.token} {self.mqttUrl} {b64decode(self.mqttinfo.encode()).decode('latin-1')}")
        return self.mqtt(self.token, "zenApp", b64decode(self.mqttinfo.encode()).decode("latin-1"), onMessage)

    async def _get_detail(self, deviceId: str) -> Any:
        payload = {"deviceId": deviceId}
        url = f"{self.zen_api}{SF_DEVICEDETAILS_PATH}"
        _LOGGER.info(f"Getting device details for [{deviceId}] ...")
        response = await self.session.post(url=url, json=payload, headers=self.headers)
        if response.ok:
            respJson = await response.json()
            _LOGGER.info(f"Got data for [{deviceId}] {len(respJson)}...")
            return respJson["data"]

            raise SessionNotInitializedError()
        _LOGGER.error(response.text)
        return None

    async def _createDeviceDefinition(self, data: dict[str, str], parent: ZendureDeviceDefinition | None = None) -> ZendureDeviceDefinition | None:
        prodName = data.get("productName")
        if not data or not (deviceKey := data.get("deviceKey")) or not prodName:
            _LOGGER.debug(
                f"Unable to get details for: {data.get('deviceId')} {prodName}"
            )
            return None


        _LOGGER.info(f"Adding device: {deviceKey} {prodName}")

        return ZendureDeviceDefinition(
            productKey=data["productKey"],
            deviceKey=deviceKey,
            deviceName=data["deviceName"],
            snNumber=data["snNumber"],
            productName=prodName,
            ip_address=data.get("ip"),
            parent=parent,
        )


    async def getDevices(self) -> dict[str, ZendureDeviceDefinition]:
        if not self.session:
            raise SessionNotInitializedError

        devices: dict[str, ZendureDeviceDefinition] = {}
        try:
            url = f"{self.zen_api}{SF_DEVICELIST_PATH}"
            _LOGGER.info("Getting device list ...")

            response = await self.session.post(url=url, headers=self.headers)
            if response.ok:
                respJson = await response.json()
                deviceInfo = respJson["data"]
                for dev in deviceInfo:
                    if (deviceId := dev["id"]) is None or dev["productName"] is None:
                        continue
                    try:
                        data = await self._get_detail(deviceId)
                        device = await self._createDeviceDefinition(data)
                        if device is None:
                            continue
                        devices[device.deviceKey] = device


                        if len(data["packDataList"]):
                            for pack in data["packDataList"]:
                                if pack.get("productName", "") == "Ace 1500":
                                    device = await self._createDeviceDefinition(pack)
                                    if device is None:
                                        continue
                                    devices[device.deviceKey] = device

                        _LOGGER.info(f"Data: {data}")
                    except Exception as e:
                        _LOGGER.error(traceback.format_exc())
                        _LOGGER.error(e)
            else:
                _LOGGER.error(f"Fetching device list failed: {response.text}")
        except Exception as e:
            _LOGGER.error(e)

        return devices

    def mqtt(self, clientId: str, username: str, password: str, onMessage: Callable) -> mqtt_client.Client:
        _LOGGER.info(f"Create mqtt client!! {clientId}")
        client = mqtt_client.Client(client_id=clientId, clean_session=False)
        client.username_pw_set(username=username, password=password)
        client.on_connect = self.onConnect
        client.on_disconnect = self.onDisconnect
        client.on_message = onMessage
        client.connect(self.mqttUrl, 1883)

        client.suppress_exceptions = True
        client.loop()
        client.loop_start()
        return client

    def onConnect(self, _client: Any, _userdata: Any, _flags: Any, _rc: Any) -> None:
        _LOGGER.info("Client has been connected")

    def onDisconnect(self, _client: Any, _userdata: Any, _rc: Any) -> None:
        _LOGGER.info("Client has been disconnected; trying to restart")
        _client.reconnect()
        _client.loop_start()


class SessionNotInitializedError(Exception):
    """Exception raised when the session is not initialized."""

    def __init__(self) -> None:
        """Initialize the exception."""
        super().__init__("Session is not initialized!")
