"""Zendure Integration api."""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable
from typing import Any, Mapping

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from paho.mqtt import client as mqtt_client
from paho.mqtt import enums as mqtt_enums

from .api_auth import api_ha
from . import mqtt_protocol
from .const import (
    CONF_MQTTLOG,
    CONF_MQTTPORT,
    CONF_MQTTPSW,
    CONF_MQTTSERVER,
    CONF_MQTTUSER,
    CONF_WIFIPSW,
    CONF_WIFISSID,
)
from .device import ZendureDevice
from .devices.ace1500 import ACE1500
from .devices.aio2400 import AIO2400
from .devices.hub1200 import Hub1200
from .devices.hub2000 import Hub2000
from .devices.hyper2000 import Hyper2000
from .devices.solarflow800 import SolarFlow800, SolarFlow800Plus, SolarFlow800Pro
from .devices.solarflow1600 import SolarFlow1600
from .devices.solarflow2400 import SolarFlow2400AC, SolarFlow2400AC_Plus, SolarFlow2400Pro
from .devices.superbasev4600 import SuperBaseV4600
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
        "solarflow 800 plus": SolarFlow800Plus,
        "solarflow 1600 ac+": SolarFlow1600,
        "solarflow 2400 ac": SolarFlow2400AC,
        "solarflow 2400 ac+": SolarFlow2400AC_Plus,
        "solarflow 2400 pro": SolarFlow2400Pro,
        "superbase v6400": SuperBaseV6400,
        "superbase v4600": SuperBaseV4600,
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

    def _setup_mqtt_client(self, client: mqtt_client.Client, srv: str, port: str, user: str, psw: str) -> None:
        try:
            client.on_connect = self.mqttConnect
            client.on_disconnect = self.mqttDisconnect
            client.on_message = self.mqttMsgCloud if client == Api.mqttCloud else self.mqttMsgLocal if client == Api.mqttLocal else self.mqttMsgDevice
            client.suppress_exceptions = True
            client.username_pw_set(user, psw)
            client.connect(srv, int(port))
            client.loop_start()
        except Exception as e:
            _LOGGER.error("Unable to connect to Zendure %s!", e)

    def Init(self, data: Mapping[str, Any], mqtt: Mapping[str, Any]) -> None:
        """Initialize Zendure Api."""
        # Shutdown existing clients
        self.shutdown()

        # Re-create client objects instead of re-initializing
        Api.mqttCloud = mqtt_client.Client(mqtt_enums.CallbackAPIVersion.VERSION2, mqtt["clientId"], False, "cloud", mqtt_enums.MQTTProtocolVersion.MQTTv31)
        Api.mqttLogging = data.get(CONF_MQTTLOG, False)
        url = mqtt["url"]
        Api.cloudServer, Api.cloudPort = url.rsplit(":", 1) if ":" in url else (url, "1883")
        self._setup_mqtt_client(Api.mqttCloud, Api.cloudServer, Api.cloudPort, mqtt["username"], mqtt["password"])

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
            Api.mqttLocal = mqtt_client.Client(mqtt_enums.CallbackAPIVersion.VERSION2, clientId, True, "local", mqtt_enums.MQTTProtocolVersion.MQTTv31)
            self._setup_mqtt_client(Api.mqttLocal, Api.localServer, Api.localPort, Api.localUser, Api.localPassword)

    @staticmethod
    async def Connect(hass: HomeAssistant, data: dict[str, Any], reload: bool) -> dict[str, Any] | None:
        """Connect to the Zendure API."""
        try:
            devices = await api_ha(hass, data)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.error("Failed to connect to Zendure API")
            return None

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

    def shutdown(self) -> None:
        """Properly shutdown MQTT clients and stop background threads."""
        for client in (self.mqttCloud, self.mqttLocal):
            if client is not None and client.is_connected():
                client.loop_stop()
                client.disconnect()

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

            else:
                _LOGGER.debug("Unknown device: %s => %s", deviceId, msg.topic)

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

                    # if device.zendure is None:
                    #     psw = hashlib.md5(device.deviceId.encode()).hexdigest().upper()[8:24]  # noqa: S324
                    #     device.zendure = mqtt_client.Client(mqtt_enums.CallbackAPIVersion.VERSION2, device.deviceId, False, "zendure")
                    #     self.mqttInit(device.zendure, Api.cloudServer, Api.cloudPort, device.deviceId, psw)

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
