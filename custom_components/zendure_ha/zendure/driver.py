"""ZendureDriver: connects to MQTT, discovers devices, routes telemetry."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import time
from collections.abc import Callable
from datetime import datetime
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace
from typing import Any, NamedTuple

import paho.mqtt.client as mqtt
from aiohttp import ClientTimeout
from bleak import BleakClient
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth, zeroconf
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

from ..const import DOMAIN
from ..device import DeviceState, DistributionDevice
from ..entity import EntityDevice
from .battery import ZendureBattery
from .device import COMMAND_CHAR, READ_CHAR, ConnectionType, ZendureDevice
from .smartmeter import ZendureSmartMeter
from .solarflow import ZendureHub, ZendureHyper, ZendureLegacy, ZendureSolarFlow

_LOGGER = logging.getLogger(__name__)

SF_HAKEY = "C*dafwArEOXK"
USER_ID = 38555  # the cloud sends this account id in its replies (captured 2026-08-24); the device seems to expect it
BRIDGE_S = 300  # a local device's reports flow to the cloud for this long after the app last asked it something

class Product(NamedTuple):
    cls: type
    family: str  # names the device (family_<serial tail>)
    pwr_max: int = 0  # the three powers belong to a battery device; a smart meter has none
    pwr_min: int = 0
    pwr_solar: int = 0
    prodkeys: tuple[str, ...] = ()  # the cloud and the BLE handshake name no product, only one of these keys


# one table for every product; the key is the product string the device reports over zenSDK, mDNS or MQTT
PRODUCTS: dict[str, Product] = {
    "solarflow2400ac": Product(ZendureSolarFlow, "SF2400", 2400, -2400, 0, ("bc8b7f",)),
    "solarflow2400ac+": Product(ZendureSolarFlow, "SF2400", 2400, -2400, 0, ("5fg27j",)),
    "solarflow2400pro": Product(ZendureSolarFlow, "SF2400", 2400, -2400, 0, ("2qe7c9",)),
    "solarflow1600ac+": Product(ZendureSolarFlow, "SF1600", 1600, -1600, 0, ("65174u",)),
    "solarflow800": Product(ZendureSolarFlow, "SF800", 800, -1000, 0, ("a4ss5p", "b1nhmc", "n8sky9")),
    "solarflow800pro": Product(ZendureSolarFlow, "SF800", 800, -1000, -1200, ("r3mn8u",)),
    "solarflow800pro2": Product(ZendureSolarFlow, "SF800", 800, -1000, -1200, ("nvyeqm",)),
    "solarflow800plus": Product(ZendureSolarFlow, "SF800", 800, -800, 0, ("8n77v3",)),
    "solarflow4000mixac+": Product(ZendureSolarFlow, "SF4000", 4000, -4000, -2400, ("gvvy0c",)),
    "solarflow4000mixpro": Product(ZendureSolarFlow, "SF4000", 4000, -4000, -2400, ("k50hwq",)),
    "solarflow3000mixac+": Product(ZendureSolarFlow, "SF3000", 3000, -3000, -2400, ("g0wyvd",)),
    "hyper2000": Product(ZendureHyper, "Hyper2000", 1200, -1200, -1600, ("gda3tb", "b3dxda", "ja72u0")),
    "solarflowpro": Product(ZendureHyper, "Hyper2000", 1200, -1200, -1600),  # what a Hyper 2000 calls itself on the broker
    "hub1200": Product(ZendureHub, "Hub1200", 1200, 0, -800, ("73bktv",)),
    "hub2000": Product(ZendureHub, "Hub2000", 1200, 0, -2400, ("a8yh63",)),
    "aio2400": Product(ZendureLegacy, "AIO2400", 1200, 0, -1200, ("ywf7hv",)),
    "ace1500": Product(ZendureLegacy, "Ace1500", 800, -900, -900, ("8bm93h",)),
    "smartmeter3ct": Product(ZendureSmartMeter, "Meter3CT", 0, 0, 0, ("c3yt68", "14yyd8")),
    "smartmeter3ct-s": Product(ZendureSmartMeter, "Meter3CTS", 0, 0, 0, ("y6hvtw",)),
    "smartmeterp1": Product(ZendureSmartMeter, "MeterP1", 0, 0, 0, ("q331b1",)),
    "smartplug": Product(ZendureSmartMeter, "Plug", 0, 0, 0, ("1dmcr8",)),
    "smartct": Product(ZendureSmartMeter, "MeterCT", 0, 0, 0, ("vv1wd7",)),
}
PRODKEYS: dict[str, str] = {key: product for product, row in PRODUCTS.items() for key in row.prodkeys}


def product_row(product: str) -> Product | None:
    # the product string is written differently per route ("SolarFlow 800 Pro" from the cloud, "solarFlow800Pro" locally)
    return PRODUCTS.get(product.lower().replace(" ", ""))


class ZendureDriver:
    """Connects to MQTT, discovers devices, routes telemetry; the coordinator turns its finds into subentries."""

    id = "zendure"  # the settings section and the subentry type of the devices
    manufacturer = "Zendure"
    ZEROCONF = [{"type": "_http._tcp.local.", "name": "zendure-*"}, {"type": "_zendure._tcp.local.", "name": "zendure-*"}]
    BLUETOOTH = [{"local_name": "Zen*", "connectable": True}]

    def __init__(self, hass, entry_id: str, all_devices: list[EntityDevice], distribution) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._all_devices = all_devices
        self.distribution = distribution
        self.devices: dict[str, ZendureSolarFlow | ZendureSmartMeter] = {}
        self.on_discovered: Callable[[dict[str, Any]], None] | None = None  # the coordinator: a new device's subentry data plus its "family"
        self.on_zeroconf: Callable[[dict[str, Any]], None] | None = None  # the coordinator: an mDNS find (sn, ip), before or after its device exists
        self._mqtt_clients: dict[ConnectionType, mqtt.Client] = {}
        self._mqtt_log = logging.getLogger(f"{DOMAIN}.mqtt")  # raw payload log; the file is opened on first use
        self._ble_unsub: Any = None  # HA's bluetooth callback registration
        self._zc_browser: AsyncServiceBrowser | None = None  # HA's zeroconf instance browsing our service types
        self._ble_probed: set[str] = set()  # addresses asked over Bluetooth who they are; once per run
        self._cloud_port = 1883
        self._bridges: dict[str, mqtt.Client] = {}  # per local device its own cloud client, logged in as the device
        self._bridge_until: dict[str, float] = {}  # per device the moment its report window closes

        self.apptoken: str = ""
        self.cloud_url: str = ""
        self.mqtt_host: str = ""
        self.mqtt_port: int = 1883
        self.mqtt_user: str = ""
        self.mqtt_psw: str = ""
        self.wifi_ssid: str = ""
        self.wifi_psw: str = ""

    async def connect(self) -> None:
        """connect to local and cloud MQTT."""

        # disconnect first
        await self.unload()

        # after setup HA no longer starts discovery flows for us (single instance), so listen for the advertisements ourselves
        try:
            self._ble_unsub = bluetooth.async_register_callback(self.hass, self._on_bluetooth, self.BLUETOOTH[0], bluetooth.BluetoothScanningMode.ACTIVE)
            aiozc = await zeroconf.async_get_async_instance(self.hass)
            self._zc_browser = AsyncServiceBrowser(aiozc.zeroconf, [m["type"] for m in self.ZEROCONF], handlers=[self._on_zeroconf])
        except Exception as err:
            _LOGGER.debug("Discovery listeners not available: %s", err)

        # connect to local MQTT
        if self.mqtt_host != "":
            self._add_mqtt_client(ConnectionType.LOCAL, self.mqtt_host, self.mqtt_port, self.mqtt_user, self.mqtt_psw)

        if not self.apptoken:
            return
        try:
            base64_url = base64.b64decode(self.apptoken).decode("utf-8")
            api_url, app_key = base64_url.rsplit(".", 1)
            timestamp = int(time.time())
            nonce = str(random.randint(10000, 99999))
            sign_params = {"appKey": app_key, "timestamp": timestamp, "nonce": nonce}
            body_str = "".join(f"{k}{v}" for k, v in sorted(sign_params.items()))
            sign = hashlib.sha1(f"{SF_HAKEY}{body_str}{SF_HAKEY}".encode()).hexdigest().upper()

            headers = {"Content-Type": "application/json", "timestamp": str(timestamp), "nonce": nonce, "clientid": "zenHa", "sign": sign}
            session = async_get_clientsession(self.hass)  # HA's shared session, never closed here
            async with session.post(f"{api_url}/api/ha/deviceList", json={"appKey": app_key}, headers=headers, timeout=ClientTimeout(total=10)) as resp:
                data = await resp.json()

            code = data.get("code")
            if code != 200 or not data.get("success"):
                _LOGGER.warning("Cloud: apikey %s... rejected, code=%s msg=%s", self.apptoken[:10], code, data.get("msg", ""))
                return
            result = data["data"]
            device_list = result.get("deviceList", []) or []
            _LOGGER.info("Cloud: apikey %s... -> %d device(s), code=%s", self.apptoken[:10], len(device_list), code)
            for dev in device_list:
                if dev_id := dev.get("deviceKey", ""):
                    self.discover(dev.get("snNumber", ""), dev.get("product", ""), dev_id, dev.get("productKey", ""), dev.get("ip", "") or "")

            # setup MQTT
            mqtt_cfg = result.get("mqtt", {})
            if mqtt_cfg and mqtt_cfg.get("url"):
                url = mqtt_cfg["url"]
                host, port = url.rsplit(":", 1) if ":" in url else (url, "1883")
                self.cloud_url = host
                self._cloud_port = int(port)
                self._add_mqtt_client(ConnectionType.CLOUD, host, int(port), mqtt_cfg.get("username", ""), mqtt_cfg.get("password", ""), mqtt_cfg.get("clientId"))
        except Exception as err:
            _LOGGER.warning("Cloud API failed for key %s... (%s)", self.apptoken[:10], err)

    async def unload(self) -> None:
        """Disconnect all MQTT clients and the discovery listeners."""
        if callable(self._ble_unsub):
            self._ble_unsub()
        self._ble_unsub = None
        if self._zc_browser is not None:
            await self._zc_browser.async_cancel()
            self._zc_browser = None
        clients = [*self._mqtt_clients.values(), *self._bridges.values()]
        for client in clients:
            client.disconnect()
        for client in clients:
            # loop_stop joins the paho thread; off the event loop, or a paho callback blocking on run_coroutine_threadsafe deadlocks HA
            await asyncio.get_running_loop().run_in_executor(None, client.loop_stop)
        self._mqtt_clients.clear()
        self._bridges.clear()
        ZendureBattery._registry.clear()  # class-level registry must not hand out stale instances after a reload

    def discover(self, sn: str, product: str, device_id: str = "", prodkey: str = "", ip: str = "", blemac: str = "") -> None:
        # a device the cloud, the broker, mDNS or Bluetooth told us about; the coordinator turns it into a subentry, which comes back through init_device
        # without a serial there is nothing to identify: a meter reader reports the meter it reads, not itself
        if self.on_discovered is None or not sn or sn in self.devices:
            return
        product = product or PRODKEYS.get(prodkey.lower(), "")  # the cloud hands over a product key, never a product
        if not (row := product_row(product)):
            _LOGGER.warning("Zendure: skipping %s: unknown product %s", sn, product)
            return
        _LOGGER.info("Found Zendure device: %s (%s)", sn, product)
        self.on_discovered({"sn": sn, "product": product, "device_id": device_id, "prodkey": prodkey, "ip": ip, "blemac": blemac, "family": row.family})

    def init_device(self, subentry_id: str, name: str, data: dict[str, Any]) -> EntityDevice | None:
        """create a Zendure Device based on the product."""
        if not (row := product_row(str(data.get("product", "")))):
            return None

        if (old := self.devices.get(data["sn"])) is not None:
            self.remove_device(old)

        powers = (row.pwr_max, row.pwr_min, row.pwr_solar) if issubclass(row.cls, DistributionDevice) else ()
        device = row.cls(self.hass, self, subentry_id, name, data, data["product"], *powers)
        if isinstance(device, DistributionDevice):
            self.distribution.devices.append(device)
            device.on_invalidate = self.distribution.on_invalidate
        self.devices[device.sn] = device
        self._all_devices.append(device)
        return device

    def remove_device(self, device: EntityDevice) -> None:
        device.unload()
        self.devices.pop(device.device_id, None)  # the identity of a Zendure device is its serial
        for collection in (self._all_devices, self.distribution.devices):
            if device in collection:
                collection.remove(device)

    @classmethod
    def device_family(cls, data: dict[str, Any]) -> str | None:
        row = product_row(str(data.get("product", "")))
        return row.family if row else None

    @classmethod
    def match_zeroconf(cls, info: Any) -> dict[str, Any] | None:
        # the hostname is zendure-{productModel}-{sn}; the serial links the find to a device, the ip is what we learn
        parts = str(info.name).split("._")[0].split("-")
        if len(parts) < 3 or parts[0].lower() != "zendure":
            return None
        return {"sn": parts[-1], "model": "-".join(parts[1:-1]), "ip": str(info.host)}

    def _on_bluetooth(self, info: Any, _change: Any) -> None:
        # HA's callback for a Zen* advertisement: one of our devices learns its address; a stranger is asked who it is, once
        if (device := self.match_bluetooth(info)) is not None:
            device.blemac = str(info.address)
            device.config_changed()
        elif (address := str(info.address)) not in self._ble_probed and not any(getattr(d, "blemac", "") == address for d in self.devices.values()):
            self._ble_probed.add(address)
            self.hass.async_create_task(self._bluetooth_found(address))

    async def _bluetooth_found(self, address: str) -> None:
        if (find := await self.probe_ble(self.hass, address)) is not None:
            self.discover(find["sn"], find.get("product", ""), find["device_id"], find["prodkey"], blemac=address)

    @classmethod
    async def probe_ble(cls, hass: Any, address: str) -> dict[str, Any] | None:
        # the start of the provisioning handshake and nothing more: connect, ack the BLESPP, and read the identity the device sends
        try:
            device = bluetooth.async_ble_device_from_address(hass, address, True)
        except Exception as err:  # no bluetooth stack in this HA
            _LOGGER.debug("Zendure BLE probe of %s: %s", address, err)
            device = None
        if device is None:
            return None
        replies: asyncio.Queue[dict] = asyncio.Queue()

        def on_notify(_sender: Any, data: bytearray) -> None:
            try:
                replies.put_nowait(json.loads(data.decode()))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass

        found: dict[str, Any] = {}
        client = None
        try:
            client = await establish_connection(BleakClient, device, address)
            await client.start_notify(READ_CHAR, on_notify)
            async with asyncio.timeout(10):
                while len(found) < 3:
                    message = await replies.get()
                    if message.get("method") == "BLESPP":
                        await client.write_gatt_char(COMMAND_CHAR, b'{"method": "BLESPP_OK"}', response=False)
                    for source in (message, message.get("properties") or {}):
                        found.update({key: str(source[key]) for key in ("productKey", "sn", "deviceId") if source.get(key)})
        except Exception as err:  # a timeout too: the device did not hand over its identity
            _LOGGER.debug("Zendure BLE probe of %s: %s", address, err)
        finally:
            if client is not None:
                await client.disconnect()
        if len(found) < 3:
            _LOGGER.warning("Zendure BLE probe of %s: no identity in the handshake, got %s", address, sorted(found))
            return None
        return {"prodkey": found["productKey"], "product": PRODKEYS.get(found["productKey"].lower(), ""), "device_id": found["deviceId"], "sn": found["sn"], "blemac": address}

    def _on_zeroconf(self, zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        # HA's zeroconf browser: resolve our services to an address on the loop, the coordinator files the ip
        if state_change is ServiceStateChange.Removed or not name.lower().startswith("zendure-"):
            return
        self.hass.async_create_task(self._zeroconf_resolve(zeroconf, service_type, name))

    async def _zeroconf_resolve(self, zc: Any, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        if await info.async_request(zc, 3000) and (addresses := info.parsed_addresses()):
            await self.zeroconf_found(name, addresses[0])

    async def zeroconf_found(self, name: str, ip: str) -> None:
        # the device tells us who it is over its local API; a meter reader reports the meter it reads, not itself
        if not (find := await self.probe(self.hass, ip)) or not (find["sn"] or find["device_id"]):
            if (host := self.match_zeroconf(SimpleNamespace(name=name, host=ip))) is None:
                return
            find = {"prodkey": "", "device_id": "", "sn": host["sn"], "product": host["model"], "ip": ip}
        elif not find["sn"] and (host := self.match_zeroconf(SimpleNamespace(name=name, host=ip))) is not None:
            find["reader_sn"] = host["sn"]
        self.discover(find["sn"], find["product"], find["device_id"], find["prodkey"], ip)
        if self.on_zeroconf is not None:
            self.on_zeroconf(find)

    @classmethod
    async def probe(cls, hass: Any, ip: str) -> dict[str, Any] | None:
        # the zenSDK report identifies the device: a SolarFlow gives sn and product, a meter reader the deviceId of its meter
        try:
            session = async_get_clientsession(hass)
            async with session.get(f"http://{ip}/properties/report", timeout=ClientTimeout(total=4)) as response:
                report = await response.json(content_type=None)
        except Exception as err:
            _LOGGER.debug("Zendure probe of %s failed: %s", ip, err)
            return None
        return {"prodkey": str(report.get("productKey") or ""), "device_id": str(report.get("deviceId") or ""), "sn": str(report.get("sn") or ""), "product": str(report.get("product") or ""), "ip": ip}

    def match_bluetooth(self, info: Any) -> EntityDevice | None:
        # the advertised name is the BLE name of one of our devices; the manufacturer data carries a serial tail that settles ties
        for d in self.devices.values():
            if not d.ble_name or not (info.name or "").startswith(d.ble_name):
                continue
            tails = [m.decode("utf-8", "ignore")[:-1] for m in (getattr(info, "manufacturer_data", None) or {}).values() if m and len(m) > 1]
            if tails and not any(t and d.sn.endswith(t) for t in tails):
                continue
            return d
        return None

    def _add_mqtt_client(self, conn: ConnectionType, host: str, port: int, username: str, password: str, client_id: str | None = None) -> None:
        if conn in self._mqtt_clients:
            return  # already connected; a config-save reload must not stack a second client
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id, userdata=conn)
            client.on_connect = self._on_mqtt_connect
            client.on_disconnect = self._on_mqtt_disconnect
            client.on_message = self._on_mqtt_message
            client.username_pw_set(username, password)
            client.connect_async(host, port)
            client.loop_start()
            self._mqtt_clients[conn] = client
            _LOGGER.info("MQTT connecting to %s:%s", host, port)
        except (ConnectionRefusedError, OSError):
            _LOGGER.warning("MQTT connection failed to %s:%s", host, port)

    def _on_mqtt_connect(self, client: Any, userdata: Any, _flags: Any, rc: Any, _props: Any) -> None:
        _LOGGER.info("MQTT [%s] connected, rc=%s", userdata, rc)
        if userdata == ConnectionType.LOCAL:
            client.subscribe("/#")  # local broker: everything, discovery needs it
        for d in self.devices.values():
            # without an MQTT address there is no topic to talk to; that device is steered over zenSDK
            if not d.device_key:
                continue
            if userdata != ConnectionType.LOCAL:
                client.subscribe(f"/+/{d.device_key}/#")  # the prodkey in the report topic is not always the one the API handed us
            # kick off the reporting, the same read BLE sends right after connecting
            client.publish(f"iot/{d.prodkey}/{d.device_key}/properties/read", json.dumps({"properties": ["getAll"]}))

    def _bridge(self, d: ZendureDevice) -> mqtt.Client | None:
        # The app reaches a local device through the cloud. One cloud client per device, logged in as that device, carries the
        # app's requests to the local broker and, for BRIDGE_S after a request, the device's reports back to the cloud.
        if (client := self._bridges.get(d.device_key)) is None and self.cloud_url and d.device_key:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, d.device_key, userdata=d)
            client.on_connect = lambda c, dev, *_: c.subscribe(f"iot/{dev.prodkey}/{dev.device_key}/#")
            client.on_message = self._on_bridge_message
            client.username_pw_set(d.device_key, hashlib.md5(d.device_key.encode()).hexdigest().upper()[8:24])
            client.connect_async(self.cloud_url, self._cloud_port)
            client.loop_start()
            self._bridges[d.device_key] = client
        return client

    def _on_bridge_message(self, _client: Any, d: ZendureDevice, msg: Any) -> None:
        # a request from the app or the cloud: hand it to the device on the local broker and open the report window
        if not msg.payload or b'"isHA"' in msg.payload or msg.topic.endswith("time-sync") or (local := self._mqtt_clients.get(ConnectionType.LOCAL)) is None:
            return
        self._bridge_until[d.device_key] = time.time() + BRIDGE_S
        local.publish(msg.topic, msg.payload)

    def _on_mqtt_disconnect(self, client: Any, userdata: Any, _flags: Any, rc: Any, _props: Any) -> None:
        _LOGGER.info("MQTT [%s] disconnected, rc=%s", userdata, rc)

    def _on_mqtt_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """Paho callback on its own network thread: hop onto the HA event loop, where all downstream work must run."""
        try:
            if msg.payload is None or not msg.payload or b'"isHA"' in msg.payload:
                return  # empty, or our own bridged copy coming back

            if self.distribution.mqtt_log:
                if not self._mqtt_log.handlers:  # opened here, on the paho thread, so the event loop never blocks
                    handler = RotatingFileHandler(self.hass.config.path(f"{DOMAIN}_mqtt.jsonl"), maxBytes=20_000_000, backupCount=3)
                    handler.setFormatter(logging.Formatter("%(message)s"))
                    self._mqtt_log.addHandler(handler)
                    self._mqtt_log.setLevel(logging.INFO)
                    self._mqtt_log.propagate = False
                self._mqtt_log.info(json.dumps({"t": time.time(), "topic": msg.topic, "payload": msg.payload.decode("utf-8", "ignore")}))

            _, prodkey, deviceid, topic = msg.topic.split("/", 3)
            payload = json.loads(msg.payload.decode("utf-8", "ignore"))
            if (d := next((d for d in self.devices.values() if d.device_key == deviceid), None)) is not None:
                # a device on zenSDK stays there: the broker still brings its reports, the commands go over HTTP
                if d.connection not in (userdata, ConnectionType.ZENSDK):
                    d.connection = userdata
                    d.mqtt = client
                if userdata == ConnectionType.LOCAL and (bridge := self._bridge(d)) is not None and time.time() < self._bridge_until.get(deviceid, 0.0):
                    bridge.publish(msg.topic, json.dumps({**payload, "isHA": True}))

                match topic:
                    case "properties/report":
                        d.update_properties(payload, time.time())  # stays on the paho thread; it hops itself when it must create something

                    case "register":
                        _LOGGER.info("Register for %s => %s", d.device_id, payload)
                        if userdata == ConnectionType.LOCAL:
                            reply = json.dumps({"token": payload.get("params", {}).get("token"), "result": 0})  # flat, exactly like the proven Brandthus reply
                            client.publish(f"iot{msg.topic}/replay", reply)  # no retain: a retained replay reboot-loops the device

                    case "function/invoke/reply":
                        if isinstance(d, ZendureSolarFlow) and payload.get("messageId") == d._msg_id:
                            d._msg_update = time.time()
                            d.set_state(DeviceState.CONFIRM)

                    case "time-sync":
                        z = datetime.now().astimezone().strftime("%z")
                        reply = json.dumps({"zoneOffset": f"{z[:3]}:{z[3:]}", "timestamp": int(time.time()), "userId": USER_ID, "messageId": payload.get("messageId", "none")})
                        client.publish(f"iot{msg.topic}/reply", reply)
            else:
                if sn := payload.get("log", {}).get("sn") or payload.get("slaveSN") or payload.get("properties", {}).get("slaveSn") or payload.get("deviceSn"):
                    if (known := self.devices.get(sn)) is None:
                        self.hass.loop.call_soon_threadsafe(self.discover, sn, str(payload.get("product", "")), deviceid, prodkey)
                    elif known.device_key != deviceid:
                        # the topic carries the MQTT address; a device that came from mDNS learns it here
                        known.device_key = deviceid
                        known.prodkey = prodkey
                        known.config_changed()

        except RuntimeError:
            pass  # loop closed during shutdown, drop the message
        except Exception as ex:
            _LOGGER.warning("MQTT message %s: %s", msg.topic, ex)
