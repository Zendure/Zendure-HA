"""Api for Zendure Integration."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

from bleak import BleakClient
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

_LOGGER = logging.getLogger(__name__)

SF_COMMAND_CHAR = "0000c304-0000-1000-8000-00805f9b34fb"
SF_NOTIFY_CHAR = "0000c305-0000-1000-8000-00805f9b34fb"


class Api:
    """Api for Zendure Integration."""

    iotUrl: str = "iot.zendure.com"
    wifi_ssid: str = ""
    wifi_psw: str = ""
    reply: dict | None = None
    discover: list[str] = []

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
            await ble_cmd({"messageId": "none", "method": "getInfo", "timestamp": str(int(time.time()))})

            await ble_cmd(
                {
                    "AM": 3,
                    # "homeId": 64847,
                    # "iotUrl": "mqtteu.zen-iot.com",
                    "iotUrl": Api.iotUrl,
                    "messageId": 1002,
                    "method": "token",
                    "password": Api.wifi_psw,
                    "ssid": Api.wifi_ssid,
                    "timeZone": "GMT+08:00",
                    # "timeZone": "GMT+01:00",
                    # "token": "1g1L5Oym2r8004oE",
                    "token": "abcdefghijklmnop",
                }
            )

            await asyncio.sleep(15)

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
