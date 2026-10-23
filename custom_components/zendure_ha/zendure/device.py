"""ZendureDevice: shared BLE transport for every Zendure device; the concrete class owns its own device loop."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from bleak import BleakClient
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from ..const import DOMAIN

if TYPE_CHECKING:
    from .driver import ZendureDriver


_LOGGER = logging.getLogger(__name__)

COMMAND_CHAR = "0000c304-0000-1000-8000-00805f9b34fb"
READ_CHAR = "0000c305-0000-1000-8000-00805f9b34fb"


def signed_convert(v) -> int:
    """Decode a 16-bit two's complement value."""
    v = int(v)
    return v - 65536 if v >= 32768 else v


def temp_convert(v) -> float:
    """Convert decikelvin to Celsius."""
    return round((float(v) - 2731) / 10, 1)


def deci_convert(v) -> float:
    """Convert a signed tenth to its unit."""
    return signed_convert(v) / 10.0


def centi_convert(v) -> float:
    """Convert a signed hundredth to its unit."""
    return signed_convert(v) / 100.0


class Command(IntEnum):
    TIMEOUT = 0
    POWER = 1
    WRITE = 2


class ConnectionType(IntEnum):
    OFFLINE = 0
    LOCAL = 1
    CLOUD = 2
    ZENSDK = 3
    BLE = 4


class ZendureDevice:
    """Zendure identity, HA registry entry and BLE transport; the concrete class runs EntityDevice.__init__ after this."""

    def __init__(self, hass: HomeAssistant, driver: ZendureDriver | None, subentry_id: str | None, name: str, data: dict[str, Any], model: str) -> None:
        self.driver = driver
        self.sn: str = data["sn"]
        self.prodkey: str = data.get("prodkey", "")
        self.device_key: str = data.get("device_id", "")  # the MQTT address, empty until a broker, cloud or BLE find fills it
        self.model = model
        self.ip: str | None = data.get("ip") or None
        self.blemac: str = data.get("blemac") or ""
        self.on_config_changed: Callable[[Any], None] | None = None  # the coordinator: persist subentry_data() after ip or blemac were learned
        sn = self.sn
        if (model.lower().startswith("solarflow") or "smart" in model.lower()) and len(sn) >= 4:
            self.ble_name: str | None = f"Zen{sn[0]}{sn[2]}{sn[3]}_{sn[-1]}"  # SolarFlow and smart meters
        elif len(sn) >= 3:
            self.ble_name = f"Zen{sn[0]}{sn[2]}_{sn[-2:]}"  # Hyper and others
        else:
            self.ble_name = None
        self._msg_id: int = 100000
        self._ble_client: BleakClient | None = None
        self._ble_replies: asyncio.Queue[dict] = asyncio.Queue()
        if driver is not None:
            # the registry entry carries what HA shows; the serial and the device key stay in the subentry
            dev = dr.async_get(hass).async_get_or_create(config_entry_id=driver.entry_id, config_subentry_id=subentry_id, identifiers={(DOMAIN, self.sn)}, name=name, manufacturer=driver.manufacturer, model=model, model_id=self.prodkey or None, connections={(dr.CONNECTION_BLUETOOTH, self.blemac)} if self.blemac else None)

            # a device migrated from 1.x is still a member of the main entry; take it out so it only belongs to its subentry
            if subentry_id and None in dev.config_entries_subentries.get(driver.entry_id, set()):
                dr.async_get(hass).async_update_device(dev.id, remove_config_entry_id=driver.entry_id, remove_config_subentry_id=None)

    def subentry_data(self) -> dict[str, Any]:
        return {"product": self.model, "sn": self.sn, "device_id": self.device_key, "prodkey": self.prodkey, "ip": self.ip or "", "blemac": self.blemac}

    def config_changed(self) -> None:
        # ip, blemac or a pack name were learned; the coordinator persists them, on the loop, since telemetry arrives on the paho thread
        if self.on_config_changed is not None:
            self.hass.loop.call_soon_threadsafe(self.on_config_changed, self)

    async def _ble_connect(self) -> bool:
        if self._ble_client and self._ble_client.is_connected:
            return True
        self._ble_client = None
        if not self.ble_name:
            return False
        try:
            if not self.blemac:
                # first contact: the discovery flow normally fills this, this is the fallback
                info = next((i for i in bluetooth.async_discovered_service_info(self.hass, True) if (i.name or "").startswith(self.ble_name)), None)
                if info is None:
                    _LOGGER.debug("BLE %s not found", self.ble_name)
                    return False
                self.blemac = info.address
                self.config_changed()
            if (device := bluetooth.async_ble_device_from_address(self.hass, self.blemac, True)) is None:
                _LOGGER.debug("BLE %s (%s) not reachable", self.ble_name, self.blemac)
                return False
            self._ble_client = await establish_connection(BleakClient, device, self.device_id)
            _LOGGER.debug("BLE connected to %s (%s)", self.ble_name, self.blemac)
            await self._ble_client.start_notify(READ_CHAR, self._ble_on_notify)
            await self._ble_wait_for("BLESPP", timeout=10)  # the notify handler acks every BLESPP with a bare BLESPP_OK
            await self._ble_command({"properties": ["getAll"], "method": "read"})  # kicks off the continuous reporting
            return True
        except Exception:
            _LOGGER.debug("BLE connect failed for %s", self.device_id, exc_info=True)
            self._ble_client = None
            return False

    async def _ble_command(self, command: dict, method: str | None = None) -> bool:
        if not self._ble_client or not self._ble_client.is_connected:
            _LOGGER.debug("BLE command not connected for %s", self.device_id)
            return False
        try:
            self._msg_id += 1
            if method is not None:
                command["method"] = method
            if "messageId" not in command:
                command["messageId"] = self._msg_id
                command["timestamp"] = int(time.time())
                command["deviceId"] = self.device_id
            _LOGGER.debug("BLE -> %s: %s", self.device_id, json.dumps(command))
            await self._ble_client.write_gatt_char(COMMAND_CHAR, json.dumps(command).encode(), response=False)
            return True
        except Exception:
            _LOGGER.debug("BLE command failed for %s", self.device_id)
            return False

    def _ble_on_notify(self, sender, data: bytearray) -> None:
        try:
            response = json.loads(data.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if response.get("method") != "report":
            _LOGGER.debug("BLE <- %s: %s", self.device_id, data.decode("utf-8", "ignore"))
        method = response.get("method")
        if method == "BLESPP" and self._ble_client:
            # ack every BLESPP with a bare BLESPP_OK, exactly like the proven scripts: no messageId, timestamp or deviceId
            asyncio.get_running_loop().create_task(self._ble_client.write_gatt_char(COMMAND_CHAR, b'{"method": "BLESPP_OK"}', response=False))
        if method == "report":
            self.update_properties(response, time.time(), True)  # telemetry is a continuous push, replies answer a request
        else:
            self._ble_replies.put_nowait(response)

    async def _ble_wait_for(self, method: str | None = None, timeout: float = 10.0) -> dict:
        try:
            while True:
                async with asyncio.timeout(timeout):
                    response = await self._ble_replies.get()
                if method is None or response.get("method") == method:
                    return response
        except TimeoutError:
            return {}
