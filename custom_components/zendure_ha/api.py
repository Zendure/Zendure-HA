"""Api for Zendure Integration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import traceback
from base64 import b64decode
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bleak import BleakClient
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.components.mqtt.async_client import AsyncMQTTClient
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from paho.mqtt import client as mqtt_client
from paho.mqtt import enums as mqtt_enums

from .const import CONF_APPTOKEN, CONF_HAKEY, CONF_MQTTPSW, CONF_MQTTSERVER, CONF_MQTTUSER, CONF_WIFIPSW, CONF_WIFISSID, DOMAIN
from .device import ZendureDevice
from .devices.ace1500 import ACE1500
from .devices.aio2400 import AIO2400
from .devices.hub1200 import Hub1200
from .devices.hub2000 import Hub2000
from .devices.hyper2000 import Hyper2000
from .devices.solarflow800 import SolarFlow800, SolarFlow800Plus, SolarFlow800Pro
from .devices.solarflow2400 import SolarFlow2400AC
from .devices.superbase import SuperBaseV4600, SuperBaseV6400
from .entity import ZendureEntities
from .smartmeter import ZendureSmartMeter

_LOGGER = logging.getLogger(__name__)

CONST_TOPIC_CNT = 4
CONST_HAKEY = "C*dafwArEOXK"
CONST_HA_OK = 200
CONST_HEADER = {"content-type": "application/json; charset=UTF-8"}
SF_COMMAND_CHAR = "0000c304-0000-1000-8000-00805f9b34fb"
SF_NOTIFY_CHAR = "0000c305-0000-1000-8000-00805f9b34fb"


class Api:
    """Api for Zendure Integration."""

    cloud = mqtt_client.Client()
    local = mqtt_client.Client()
    devices: dict[str, ZendureEntities] = {}
    models: dict[str, tuple[str, type[ZendureEntities]]] = {
        "a4ss5p": ("SolarFlow 800", SolarFlow800),
        "b1nhmc": ("SolarFlow 800", SolarFlow800),
        "n8sky9": ("SolarFlow 800AC", SolarFlow800),
        "8n77v3": ("SolarFlow 800Plus", SolarFlow800Plus),
        "r3mn8u": ("SolarFlow 800Pro", SolarFlow800Pro),
        "bc8b7f": ("SolarFlow 2400AC", SolarFlow2400AC),
        "2qe7c9": ("SolarFlow 2400Pro", SolarFlow2400AC),
        "5fg27j": ("SolarFlow 2400AC+", SolarFlow2400AC),
        "c3yt68": ("SmartMeter 3CT", ZendureSmartMeter),
        "y6hvtw": ("SmartMeter 3CT-S", ZendureSmartMeter),
        "q331b1": ("SmartMeter P1", ZendureSmartMeter),
        "1dmcr8": ("SmartPlug", ZendureSmartMeter),
        "vv1wd7": ("Smart Ct", ZendureSmartMeter),
        "gda3tb": ("Hyper 2000", Hyper2000),
        "b3dxda": ("Hyper 2000", Hyper2000),
        "ja72u0": ("Hyper 2000", Hyper2000),
        "73bktv": ("Hub 1200", Hub1200),
        "a8yh63": ("Hub 2000", Hub2000),
        "ywf7hv": ("AIO 2400", AIO2400),
        "8bm93h": ("ACE 1500", ACE1500),
        "v4600": ("SuperBase v4600", SuperBaseV4600),
        "v6400": ("SuperBase v6400", SuperBaseV6400),
    }

    async def async_init(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the Zendure API."""
        self.hass = hass
        self.entry = entry
        self._messageid = 1000000

        # Get wifi settings
        self.wifissid = entry.data.get(CONF_WIFISSID)
        self.wifipsw = entry.data.get(CONF_WIFIPSW)

        # Init Mqtt
        if (data := await self.ZendureCloud()) is not None and (mqtt := data.get("mqtt")) is not None and (deviceList := data.get("deviceList")) is not None:
            self.topics = [f"/{d['productKey']}/{d['deviceKey']}/#" for d in deviceList]
            self.topics.append("/Q331b1/A0r9v5UC/#")
            self.mqtt_init(self.cloud, mqtt.get("url"), mqtt.get("username"), mqtt.get("password"), mqtt.get("clientId"))
        if entry.data.get(CONF_MQTTSERVER) is not None:
            self.mqtt_init(self.local, entry.data.get(CONF_MQTTSERVER), entry.data.get(CONF_MQTTUSER), entry.data.get(CONF_MQTTPSW), DOMAIN)
        _LOGGER.debug("Zendure API initialized")

    async def ZendureCloud(self) -> dict[str, Any] | None:
        session = async_get_clientsession(self.hass)

        if (token := self.entry.data.get(CONF_APPTOKEN)) is not None and len(token) > 1:
            base64_url = b64decode(str(token)).decode("utf-8")
            api_url, appKey = base64_url.rsplit(".", 1)
        else:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_zendure_token")

        try:
            # Prepare signature parameters
            body = {"appKey": appKey}
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
            sign_str = f"{CONST_HAKEY}{body_str}{CONST_HAKEY}"
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
            if data.get("code") != CONST_HA_OK or not data.get("success", False) or (json := data["data"]) is None:
                _LOGGER.debug(f"Zendure API response: {data.get('code')} Message: {data.get('msg')}")
                return None
            return dict(json)

        except Exception as e:
            _LOGGER.error(f"Unable to connect to Zendure {e}!")
            _LOGGER.error(traceback.format_exc())
            return None

    @staticmethod
    async def mqtt_connect(hass: HomeAssistant, input: dict[str, Any], cloud: bool) -> str | None:
        return None

    def mqtt_init(self, client: mqtt_client.Client, server: str | None, user: str | None, password: str | None, clientid: str | None) -> None:
        """Connect to the Zendure MQTT server."""
        if server is None or user is None or password is None:
            return
        client.__init__(mqtt_enums.CallbackAPIVersion.VERSION2, clientid, True, userdata=client.user_data_get())
        client.username_pw_set(user, password)
        client.on_connect = self.mqtt_connect_cloud if client == self.cloud else self.mqtt_connect_local
        client.on_disconnect = self.mqtt_disconnect
        client.on_message = self.mqtt_message
        client.connect(server, 1883)
        client.loop_start()

    def mqtt_connect_cloud(self, client: Any, _userdata: Any, _flags: Any, rc: Any, _props: Any) -> None:
        _LOGGER.info(f"Client connected to cloud MQTT broker, return code: {rc}")
        for topic in self.topics:
            client.subscribe(topic)

    def mqtt_connect_local(self, client: Any, _userdata: Any, _flags: Any, rc: Any, _props: Any) -> None:
        _LOGGER.info(f"Client connected to local MQTT broker, return code: {rc}")
        client.subscribe("/#")

    def mqtt_disconnect(self, _client: Any, userdata: Any, _flags: Any, rc: Any, _props: Any) -> None:
        # _LOGGER.info(f"Client {userdata} disconnected to MQTT broker, return code: {rc}")
        pass

    def mqttPublish(self, client: mqtt_client.Client, deviceId: str, topic: str, command: Any) -> None:
        self._messageid += 1
        command["messageId"] = self._messageid
        command["deviceId"] = deviceId
        command["timestamp"] = int(datetime.now().timestamp())
        payload = json.dumps(command, default=lambda o: o.__dict__)
        client.publish(topic, payload=payload)

    def mqtt_message(self, client: mqtt_client.Client, _userdata: Any, msg: mqtt_client.MQTTMessage) -> None:
        """Handle Zendure mqtt messages."""
        if msg.payload is None or not msg.payload:
            return
        try:
            # Validate topic format before accessing indices
            if len(topics := msg.topic.split("/", 3)) < CONST_TOPIC_CNT:
                _LOGGER.warning("Invalid MQTT topic format: %s (expected 4 segments)", msg.topic)
                return

            # deserialize payload
            deviceId = topics[2]
            try:
                payload = json.loads(msg.payload)
                if "isHA" in payload:
                    return
                _LOGGER.info("Topic: %s => %s", msg.topic, payload)
            except json.JSONDecodeError as err:
                _LOGGER.error("Failed to decode JSON from device %s: %s", deviceId, err)
                return
            except UnicodeDecodeError as err:
                _LOGGER.error("Failed to decode payload encoding from device %s: %s", deviceId, err)
                return

            if (topic := topics[3]) == "time-sync":
                reply = json.dumps({"zoneOffset": "01:00", "timestamp": int(datetime.now().timestamp()), "messageId": payload.get("messageId", "none")})
                client.publish(f"iot{msg.topic}/reply", reply)
                return

            match topic:
                case "properties/report":
                    asyncio.run_coroutine_threadsafe(self.mqtt_device(topics[1], deviceId, payload), self.hass.loop)
                case "register":
                    if (params := payload.get("params")) is not None and (token := params.get("token")) is not None:
                        self.mqttPublish(client, topics[2], f"iot{msg.topic}/replay", {"token": token, "result": 0})
                case "time-sync":
                    self.mqttPublish(client, topics[2], f"iot{msg.topic}/replay", {"zoneOffset": "01:00"})
                case "function/invoke/reply" | "properties/write/reply":
                    if (device := self.devices.get(deviceId, None)) is not None:
                        device.ready = datetime.min
                case "log":
                    if self.devices.get(deviceId, None) is None:
                        asyncio.run_coroutine_threadsafe(self.mqtt_device(topics[1], deviceId, payload), self.hass.loop)
                case _:
                    pass

            #     # if self.mqttLogging:
            #     # _LOGGER.info("Topic: %s => %s", msg.topic.replace(device.deviceId, device.name).replace(device.snNumber, "snxxx"), payload)
            # else:
            #     asyncio.run_coroutine_threadsafe(self.mqtt_device(topics[1].lower(), deviceId, payload), self.hass.loop)

        except Exception as err:
            _LOGGER.error(f"Error mqtt_message_received {err}!")
            _LOGGER.error(traceback.format_exc())

    async def mqtt_device(self, prodKey: str, deviceId: str, payload: dict) -> None:
        """Get device by ID."""
        if (device := self.devices.get(deviceId, None)) is not None:
            await device.entityRead(payload)
        elif (prod := self.models.get(prodKey.lower())) and ((sn := payload.get("deviceSn")) or ((lg := payload.get("log")) and (sn := lg.get("sn")))):
            _LOGGER.info("New device found: %s => %s", deviceId, prodKey)
            self.devices[deviceId] = prod[1](self.hass, deviceId, sn, prod[0], prodKey)
        else:
            _LOGGER.debug("Unknown device: %s => %s", deviceId, prodKey)

    # @staticmethod
    # async def IotToHA(discovery_info: BluetoothServiceInfoBleak) -> bool:
    #     """Initialize the Zendure Bluetooth API."""
    #     try:
    #         if discovery_info.address in Api.discover:
    #             _LOGGER.debug(f"Device {discovery_info.address} already configured")
    #             return True
    #         Api.discover.append(discovery_info.address)
    #         client = BleakClient(discovery_info.device)
    #         event = threading.Event()

    #         def ble_notify_rx(_bleakGATTCharacteristic: Any, data: bytearray) -> None:
    #             Api.reply = json.loads(data.decode("utf8"))
    #             _LOGGER.debug(f"BLE notify received: {Api.reply}")
    #             event.set()

    #         async def ble_cmd(command: Any) -> None:
    #             try:
    #                 event.clear()
    #                 Api.reply = None
    #                 payload = json.dumps(command, default=lambda o: o.__dict__)
    #                 b = bytearray()
    #                 b.extend(map(ord, payload))
    #                 await client.write_gatt_char(SF_COMMAND_CHAR, b, response=False)
    #                 event.wait(timeout=15)
    #             except Exception as err:
    #                 _LOGGER.warning(f"BLE error: {err}")

    #         await client.connect()
    #         await client.start_notify(SF_NOTIFY_CHAR, ble_notify_rx)
    #         await ble_cmd({"messageId": "none", "method": "getInfo", "timestamp": str(int(time.time()))})

    #         await ble_cmd(
    #             {
    #                 "AM": 3,
    #                 "iotUrl": Api.iotUrl,
    #                 "messageId": 1002,
    #                 "method": "token",
    #                 "password": Api.wifi_psw,
    #                 "ssid": Api.wifi_ssid,
    #                 "timeZone": "GMT+08:00",
    #                 "token": "abcdefghijklmnop",
    #             }
    #         )

    #         await asyncio.sleep(25)

    #         await ble_cmd(
    #             {
    #                 "messageId": 1003,
    #                 "method": "station",
    #             }
    #         )

    #         await client.stop_notify(SF_NOTIFY_CHAR)
    #         await client.disconnect()
    #     except Exception as err:
    #         _LOGGER.warning(f"BLE error: {err}")
    #         Api.discover.remove(discovery_info.address)
    #         return False

    #     Api.discover.remove(discovery_info.address)
    #     return True

    # @staticmethod
    # def fallback_start(device: ZendureDevice) -> None:
    #     """Use fallback communication for device."""
    #     if (d := Api.recover.get(device.deviceId)) is None:
    #         Api.recover[device.deviceId] = d = FallbackData(device, None, None)
    #         d.url = f"Zendure-{device.model.replace(' ', '')}-{device.deviceSn}.local" if device.zenSdk else None

    # @staticmethod
    # async def fallback_check(hass: HomeAssistant) -> None:
    #     """Try to use fallback communication."""
    #     async with async_get_clientsession(hass, verify_ssl=False) as session:
    #         for d in Api.recover.values():
    #             d.connected = False
    #             if d.url is not None:
    #                 try:
    #                     async with session.get(f"http://{d.url}/properties/report", headers=CONST_HEADER) as resp:
    #                         payload = json.loads(await resp.text())
    #                         d.device.entityRead(payload)
    #                         d.connected = True
    #                 except Exception as e:
    #                     _LOGGER.warning(f"Fetch error from {d.url}: {e}")

    #             # Try BLE if URL fetch failed
    #             if not d.connected and len(d.device.bleMac) > 0 and d.next_ble < datetime.now():
    #                 try:
    #                     d.ble = BleakClient(d.device.bleMac)
    #                     await d.ble.connect()
    #                     await d.ble.start_notify(SF_NOTIFY_CHAR, fallback_ble)
    #                     payload = json.dumps({"messageId": "none", "method": "getInfo", "timestamp": str(int(time.time()))}, default=lambda o: o.__dict__)
    #                     b = bytearray()
    #                     b.extend(map(ord, payload))
    #                     await d.ble.write_gatt_char(SF_COMMAND_CHAR, b, response=False)

    #                 except Exception as e:
    #                     _LOGGER.warning(f"Connect ble error: {e}")
    #                     d.ble = None
    #                     d.next_ble = datetime.now() + timedelta(minutes=1)

    # @staticmethod
    # async def fallback_stop(device: ZendureDevice) -> None:
    #     """Stop fallback communication."""
    #     if (d := Api.recover.get(device.deviceId)) is not None:
    #         if d.ble is not None:
    #             await d.ble.disconnect()
    #         Api.recover.pop(device.deviceId, None)

    # @staticmethod
    # def fallback_power(device: ZendureDevice, power: int) -> None:
    #     """Use fallback communication to set power."""

    # @staticmethod
    # def fallback_ble(_bleakGATTCharacteristic: Any, data: bytearray) -> None:
    #     """Fallback bluetooth response."""
    #     payload = json.loads(data.decode("utf8"))
    #     if (deviceId := payload.get("deviceId")) is not None and (d := Api.recover.get(deviceId)) is not None:
    #         d.device.entityRead(payload)
    #         d.connected = True
    #         d.next_ble = datetime.now()

    #     _LOGGER.debug(f"BLE notify received: {payload}")


@dataclass
class FallbackData:
    """Data structure for fallback communication."""

    device: ZendureDevice
    url: str | None
    ble: BleakClient | None
    connected: bool = False
    next_ble: datetime = datetime.min
