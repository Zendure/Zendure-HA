"""Api for Zendure Integration."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from bleak import BleakClient
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .device import DeviceState, ZendureDevice

_LOGGER = logging.getLogger(__name__)

CONST_HEADER = {"content-type": "application/json; charset=UTF-8"}
SF_COMMAND_CHAR = "0000c304-0000-1000-8000-00805f9b34fb"
SF_NOTIFY_CHAR = "0000c305-0000-1000-8000-00805f9b34fb"


class Api:
    """Api for Zendure Integration."""

    iotUrl: str = "iot.zendure.com"
    wifi_ssid: str = ""
    wifi_psw: str = ""
    reply: dict | None = None
    discover: list[str] = []
    recover: dict[str, FallbackData] = {}

    @staticmethod
    async def IotToHA(discovery_info: BluetoothServiceInfoBleak) -> bool:
        """Initialize the Zendure Bluetooth API."""
        try:
            if discovery_info.address in Api.discover:
                _LOGGER.debug(f"Device {discovery_info.address} already configured")
                return True
            Api.discover.append(discovery_info.address)
            client = BleakClient(discovery_info.device)
            event = threading.Event()

            def ble_notify_rx(_bleakGATTCharacteristic: Any, data: bytearray) -> None:
                Api.reply = json.loads(data.decode("utf8"))
                _LOGGER.debug(f"BLE notify received: {Api.reply}")
                event.set()

            async def ble_cmd(command: Any) -> None:
                try:
                    event.clear()
                    Api.reply = None
                    payload = json.dumps(command, default=lambda o: o.__dict__)
                    b = bytearray()
                    b.extend(map(ord, payload))
                    await client.write_gatt_char(SF_COMMAND_CHAR, b, response=False)
                    event.wait(timeout=15)
                except Exception as err:
                    _LOGGER.warning(f"BLE error: {err}")

            await client.connect()
            await client.start_notify(SF_NOTIFY_CHAR, ble_notify_rx)
            await ble_cmd({"messageId": "none", "method": "getInfo", "timestamp": str(int(time.time()))})

            await ble_cmd(
                {
                    "AM": 3,
                    "iotUrl": Api.iotUrl,
                    "messageId": 1002,
                    "method": "token",
                    "password": Api.wifi_psw,
                    "ssid": Api.wifi_ssid,
                    "timeZone": "GMT+08:00",
                    "token": "abcdefghijklmnop",
                }
            )

            await asyncio.sleep(25)

            await ble_cmd(
                {
                    "messageId": 1003,
                    "method": "station",
                }
            )

            await client.stop_notify(SF_NOTIFY_CHAR)
            await client.disconnect()
        except Exception as err:
            _LOGGER.warning(f"BLE error: {err}")
            Api.discover.remove(discovery_info.address)
            return False

        Api.discover.remove(discovery_info.address)
        return True

    @staticmethod
    def fallback_start(device: ZendureDevice) -> None:
        """Use fallback communication for device."""
        if (d := Api.recover.get(device.deviceId)) is None:
            Api.recover[device.deviceId] = d = FallbackData(device, None, None)
            d.url = f"Zendure-{device.model.replace(' ', '')}-{device.deviceSn}.local" if device.zenSdk else None

    @staticmethod
    async def fallback_check(hass: HomeAssistant) -> None:
        """Try to use fallback communication."""
        async with async_get_clientsession(hass, verify_ssl=False) as session:
            for d in Api.recover.values():
                d.connected = False
                if d.url is not None:
                    try:
                        async with session.get(f"http://{d.url}/properties/report", headers=CONST_HEADER) as resp:
                            payload = json.loads(await resp.text())
                            d.device.entityRead(payload)
                            d.connected = True
                    except Exception as e:
                        _LOGGER.warning(f"Fetch error from {d.url}: {e}")

                # Try BLE if URL fetch failed
                if not d.connected and len(d.device.bleMac) > 0 and d.next_ble < datetime.now():
                    try:
                        d.ble = BleakClient(d.device.bleMac)
                        await d.ble.connect()
                        await d.ble.start_notify(SF_NOTIFY_CHAR, fallback_ble)
                        payload = json.dumps({"messageId": "none", "method": "getInfo", "timestamp": str(int(time.time()))}, default=lambda o: o.__dict__)
                        b = bytearray()
                        b.extend(map(ord, payload))
                        await d.ble.write_gatt_char(SF_COMMAND_CHAR, b, response=False)

                    except Exception as e:
                        _LOGGER.warning(f"Connect ble error: {e}")
                        d.ble = None
                        d.next_ble = datetime.now() + timedelta(minutes=1)

    @staticmethod
    async def fallback_stop(device: ZendureDevice) -> None:
        """Stop fallback communication."""
        if (d := Api.recover.get(device.deviceId)) is not None:
            if d.ble is not None:
                await d.ble.disconnect()
            Api.recover.pop(device.deviceId, None)

    @staticmethod
    def fallback_power(device: ZendureDevice, power: int) -> None:
        """Use fallback communication to set power."""

    @staticmethod
    def fallback_ble(_bleakGATTCharacteristic: Any, data: bytearray) -> None:
        """Fallback bluetooth response."""
        payload = json.loads(data.decode("utf8"))
        if (deviceId := payload.get("deviceId")) is not None and (d := Api.recover.get(deviceId)) is not None:
            d.device.entityRead(payload)
            d.connected = True
            d.next_ble = datetime.now()

        _LOGGER.debug(f"BLE notify received: {payload}")


@dataclass
class FallbackData:
    """Data structure for fallback communication."""

    device: ZendureDevice
    url: str | None
    ble: BleakClient | None
    connected: bool = False
    next_ble: datetime = datetime.min
