"""Zendure Integration api."""

from __future__ import annotations

import binascii
import hashlib
import json
import logging
import secrets
import traceback
from base64 import b64decode
from collections.abc import Callable
from datetime import datetime
from typing import Any, Mapping

from aiohttp import ClientTimeout
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from paho.mqtt import client as mqtt_client
from paho.mqtt import enums as mqtt_enums

from .const import (
    CONF_APPTOKEN,
    CONF_DEVICE_IP,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    CONF_DEVICE_SN,
    CONF_DEVICES,
    CONF_HAKEY,
    CONF_LOCAL_ONLY,
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
from .devices.solarflow800 import SolarFlow800, SolarFlow800Plus, SolarFlow800Pro
from .devices.solarflow1600 import SolarFlow1600
from .devices.solarflow2400 import (
    SolarFlow2400AC,
    SolarFlow2400AC_Plus,
    SolarFlow2400Pro,
)
from .devices.solarflow4000 import SolarFlow4000AC_Plus
from .devices.superbasev4600 import SuperBaseV4600
from .devices.superbasev6400 import SuperBaseV6400

_LOGGER = logging.getLogger(__name__)

ZENDURE_MANAGER_STORAGE_VERSION = 1
ZENDURE_DEVICES = "devices"


class ApiError(Exception):
    """A Zendure API/setup failure with a translation key for the config flow."""

    def __init__(self, key: str, detail: str = "") -> None:
        self.key = key
        self.detail = detail
        super().__init__(key)


class Api:
    """Zendure API class."""

    createdevice: dict[str, Callable[[HomeAssistant, str, str, Any], ZendureDevice]] = {
        "ace1500": ACE1500,
        "aio2400": AIO2400,
        "solarflowaiozy": AIO2400,
        "hub1200": Hub1200,
        "solarflow2.0": Hub1200,
        "hub2000": Hub2000,
        "solarflowhub2000": Hub2000,
        "hyper2000": Hyper2000,
        "hyper2000_3.0": Hyper2000,
        "solarflow800": SolarFlow800,
        "solarflow800pro": SolarFlow800Pro,
        "solarflow800pro2": SolarFlow800Pro,
        "solarflow800plus": SolarFlow800Plus,
        "solarflow1600ac+": SolarFlow1600,
        "solarflow2400ac": SolarFlow2400AC,
        "solarflow2400ac+": SolarFlow2400AC_Plus,
        "solarflow2400pro": SolarFlow2400Pro,
        "solarflow4000ac+": SolarFlow4000AC_Plus,
        # SF4000 Mix reports its model as "solarFlow4000MixAC+" locally, it's the same hardware as the AC+
        "solarflow4000mixac+": SolarFlow4000AC_Plus,
        "superbasev6400": SuperBaseV6400,
        "superbasev4600": SuperBaseV4600,
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

        # `mqtt` carries the cloud broker credentials returned by the Zendure cloud
        # API. The local (zenSDK) setup makes no cloud call, so it is empty and that
        # device is driven entirely over its on-device HTTP API - no MQTT needed.
        # Only connect the cloud broker when those credentials are present.
        if mqtt:
            Api.mqttCloud.__init__(mqtt_enums.CallbackAPIVersion.VERSION2, mqtt["clientId"], False, "cloud", mqtt_enums.MQTTProtocolVersion.MQTTv31)
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
            self.mqttLocal.__init__(mqtt_enums.CallbackAPIVersion.VERSION2, clientId, True, "local", mqtt_enums.MQTTProtocolVersion.MQTTv31)
            self.mqttInit(self.mqttLocal, Api.localServer, Api.localPort, Api.localUser, Api.localPassword)

    @staticmethod
    async def Connect(hass: HomeAssistant, data: dict[str, Any], reload: bool) -> dict[str, Any] | None:
        """Connect to the Zendure API."""
        # Local (cloud-free) setup: the device list is described entirely by the
        # config entry, so there is no cloud call to make. The device talks to us
        # over its on-device Zen SDK HTTP API instead.
        if data.get(CONF_LOCAL_ONLY):
            return Api.localDeviceList(data)

        try:
            devices = await Api.ApiHA(hass, data)
        except ApiError:
            # During a coordinator reload we must not surface the error; fall back to
            # the last-known device list from storage. The config flow (reload=False)
            # wants the specific ApiError so it can show a precise message.
            if not reload:
                raise
            _LOGGER.error("Failed to connect to Zendure API")
            devices = None

        # Open the storage
        if reload:
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

        if (token := data.get(CONF_APPTOKEN)) is None or len(token) <= 1:
            raise ApiError("malformed_token")
        try:
            base64_url = b64decode(str(token)).decode("utf-8")
            api_url, appKey = base64_url.rsplit(".", 1)
        except (binascii.Error, UnicodeDecodeError, ValueError) as err:
            _LOGGER.error("Malformed Zendure token: %s", err)
            raise ApiError("malformed_token") from err

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
        except ApiError:
            raise
        except Exception as e:
            _LOGGER.error("Unable to reach Zendure API %s!", e)
            _LOGGER.error(traceback.format_exc())
            raise ApiError("cannot_connect", str(e)) from e

        if data.get("code") != 200 or not data.get("success", False):
            msg = str(data.get("msg", "")) or f"code {data.get('code')}"
            _LOGGER.error("Zendure API rejected the request: %s", data)
            raise ApiError("api_rejected", msg)
        if (result := data.get("data")) is None:
            _LOGGER.error("Zendure API returned no data: %s", data)
            raise ApiError("api_rejected", "no data returned")
        if len(result.get("deviceList", [])) == 0:
            _LOGGER.error("Zendure API does not reply any devices: %s", data)
            raise ApiError("no_devices")
        if len(result.get("mqtt", {})) == 0:
            _LOGGER.error("Zendure API does not reply any mqtt info: %s", data)
            raise ApiError("no_mqtt")
        return dict(result)

    @staticmethod
    def localDeviceList(data: Mapping[str, Any]) -> dict[str, Any]:
        """Build a cloud-free device list from a local config entry."""
        # Mirrors the shape ApiHA returns so the rest of the integration is unaware
        # whether the device came from the cloud or from local config. The empty
        # mqtt block signals the manager to skip cloud MQTT setup.
        deviceList = []
        for dev in data.get(CONF_DEVICES, []):
            sn = str(dev.get(CONF_DEVICE_SN, "")).strip()
            model = str(dev.get(CONF_DEVICE_MODEL, "")).strip()
            ip = str(dev.get(CONF_DEVICE_IP, "")).strip()
            name = str(dev.get(CONF_DEVICE_NAME, "") or model).strip()
            if not sn or not ip:
                continue
            deviceList.append({
                "deviceKey": sn,
                "productModel": model,
                "productKey": "local",
                "snNumber": sn,
                "deviceName": name,
                "ip": ip,
                "local": True,
            })

        if not deviceList:
            # Mirrors ApiHA's ApiError("no_devices") diagnostic: a local entry with no
            # usable devices would otherwise "load successfully" with zero entities and
            # no indication in the log of why.
            _LOGGER.error("Local Zendure entry has no usable devices in its stored device list: %s", data.get(CONF_DEVICES, []))

        return {
            "deviceList": deviceList,
            "mqtt": {},
        }

    @staticmethod
    async def testLocalDevice(hass: HomeAssistant, ip: str) -> tuple[str, str]:
        """Validate a local device via its Zen SDK report; return (serial, product)."""
        # Raises ApiError with a config-flow translation key on failure. The product
        # string (e.g. "solarFlow4000MixAC+") lets the config flow auto-detect the model.
        session = async_get_clientsession(hass, verify_ssl=False)
        try:
            response = await session.get(f"http://{ip}/properties/report", timeout=ClientTimeout(total=5))
            payload = await response.json(content_type=None)
        except Exception as e:
            _LOGGER.error("Unable to reach local Zendure device at %s: %s", ip, e)
            raise ApiError("cannot_connect_local", str(e)) from e

        if not isinstance(payload, dict) or not (sn := payload.get("sn")):
            _LOGGER.error("Local Zendure device at %s returned an unexpected report: %s", ip, payload)
            raise ApiError("no_local_report")
        return str(sn), str(payload.get("product", ""))

    def mqttInit(self, client: mqtt_client.Client, srv: str, port: str, user: str, psw: str) -> None:
        try:
            client.on_connect = self.mqttConnect
            client.on_disconnect = self.mqttDisconnect
            client.on_message = self.mqttMsgCloud if client == self.mqttCloud else self.mqttMsgLocal if client == self.mqttLocal else self.mqttMsgDevice
            client.suppress_exceptions = True
            client.username_pw_set(user, psw)
            client.connect(srv, int(port))
            client.loop_start()
        except Exception as e:
            _LOGGER.error("Unable to connect to Zendure %s!", e)

    def mqttConnect(self, client: Any, userdata: Any, _flags: Any, rc: Any, _props: Any) -> None:
        _LOGGER.info("Client %s connected to MQTT broker, return code: %s", userdata, rc)
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
        _LOGGER.info("Client %s disconnected to MQTT broker, return code: %s", userdata, rc)

    def mqttMsgCloud(self, client: Any, _userdata: Any, msg: Any) -> None:
        if msg.payload is None or not msg.payload:
            return
        try:
            topics = msg.topic.split("/", 3)

            # Validate topic format before accessing indices
            if len(topics) < 4:
                _LOGGER.warning("Invalid MQTT topic format: %s (expected 4 segments)", msg.topic)
                return

            deviceId = topics[2]

            if (device := self.devices.get(deviceId, None)) is not None:
                try:
                    payload = json.loads(msg.payload.decode())
                except json.JSONDecodeError as err:
                    _LOGGER.error("Failed to decode JSON from device %s: %s", deviceId, err)
                    return
                except UnicodeDecodeError as err:
                    _LOGGER.error("Failed to decode payload encoding from device %s: %s", deviceId, err)
                    return

                if "isHA" in payload:
                    return

                if self.mqttLogging:
                    _LOGGER.info("Topic: %s => %s", msg.topic.replace(device.deviceId, device.name).replace(device.snNumber, "snxxx"), payload)

                if device.mqttMessage(topics[3], payload) and device.mqtt != client:
                    device.mqtt = client
                    device.setStatus()

        except Exception:
            _LOGGER.exception("Unexpected error in MQTT cloud message handler")

    def mqttMsgLocal(self, client: Any, _userdata: Any, msg: Any) -> None:
        if msg.payload is None or not msg.payload or len(self.devices) == 0:
            return
        try:
            topics = msg.topic.split("/", 3)

            # Validate topic format before accessing indices
            if len(topics) < 4:
                _LOGGER.warning("Invalid local MQTT topic format: %s (expected 4 segments)", msg.topic)
                return

            deviceId = topics[2]

            if (device := self.devices.get(deviceId, None)) is not None:
                try:
                    payload = json.loads(msg.payload.decode())
                except json.JSONDecodeError as err:
                    _LOGGER.error("Failed to decode JSON from local device %s: %s", deviceId, err)
                    return
                except UnicodeDecodeError as err:
                    _LOGGER.error("Failed to decode local payload encoding from device %s: %s", deviceId, err)
                    return

                if "isHA" in payload:
                    return

                if self.mqttLogging:
                    _LOGGER.info("Local topic: %s => %s", msg.topic.replace(device.deviceId, device.name).replace(device.snNumber, "snxxx"), payload)

                if device.mqttMessage(topics[3], payload):
                    if device.mqtt != client:
                        device.mqtt = client
                        device.setStatus()

                    if device.zendure is None:
                        psw = hashlib.md5(device.deviceId.encode()).hexdigest().upper()[8:24]  # noqa: S324
                        device.zendure = mqtt_client.Client(mqtt_enums.CallbackAPIVersion.VERSION2, device.deviceId, False, "zendure")
                        self.mqttInit(device.zendure, Api.cloudServer, Api.cloudPort, device.deviceId, psw)

                    if device.zendure is not None and device.zendure.is_connected():
                        payload["isHA"] = True
                        device.zendure.publish(msg.topic, json.dumps(payload, default=lambda o: o.__dict__))
            else:
                _LOGGER.debug("Local message from unknown device %s: %s", msg.topic, deviceId)

        except Exception:
            _LOGGER.exception("Unexpected error in MQTT local message handler")

    def mqttMsgDevice(self, _client: Any, _userdata: Any, msg: Any) -> None:
        if msg.payload is None or not msg.payload:
            return
        try:
            topics = msg.topic.split("/", 3)
            deviceId = topics[2]

            if self.devices.get(deviceId, None) is not None and topics[0] == "iot":
                self.mqttLocal.publish(msg.topic, msg.payload)

        except Exception as err:
            _LOGGER.error(err)
