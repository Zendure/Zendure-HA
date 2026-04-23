"""BLE transport layer for Zendure."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from bleak import BleakClient
from bleak.exc import BleakError
from homeassistant.components import bluetooth, persistent_notification

try:
    from bleak_retry_connector import establish_connection
except ImportError:
    establish_connection = None

from paho.mqtt import client as mqtt_client

if TYPE_CHECKING:
    from .device import ZendureDevice

_LOGGER = logging.getLogger(__name__)

SF_COMMAND_CHAR = "0000c304-0000-1000-8000-00805f9b34fb"


def _scanner_source(scanner_device: Any) -> str | None:
    """Extract scanner source identifier from a BluetoothScannerDevice-like object."""
    source = getattr(scanner_device, "source", None)
    if source:
        return str(source)

    if scanner := getattr(scanner_device, "scanner", None):
        source = getattr(scanner, "source", None)
        if source:
            return str(source)

    if service_info := getattr(scanner_device, "service_info", None):
        source = getattr(service_info, "source", None)
        if source:
            return str(source)

    return None


def _scanner_ble_device(scanner_device: Any) -> Any | None:
    """Extract BLEDevice from a BluetoothScannerDevice-like object."""
    device = getattr(scanner_device, "ble_device", None)
    if device is not None:
        return device

    device = getattr(scanner_device, "device", None)
    if device is not None:
        return device

    if service_info := getattr(scanner_device, "service_info", None):
        device = getattr(service_info, "device", None)
        if device is not None:
            return device

    return None


def ble_mac(device: ZendureDevice) -> str | None:
    """Get BLE MAC address from device connections."""
    if (conn := device.attr_device_info.get("connections", None)) is not None:
        for connection_type, mac_address in conn:
            if connection_type == "bluetooth":
                return mac_address
    return None


def ble_sources(device: ZendureDevice) -> list[str]:
    """Get available Bluetooth source identifiers from Home Assistant."""
    sources: set[str] = set()
    mac = ble_mac(device)

    # Prefer scanner sources for this specific device.
    try:
        if mac and (scanner_devices_by_address := getattr(bluetooth, "async_scanner_devices_by_address", None)):
            for scanner_device in scanner_devices_by_address(device.hass, mac, True):
                if source := _scanner_source(scanner_device):
                    sources.add(source)
    except Exception as err:
        _LOGGER.debug("Could not read bluetooth scanner sources for %s: %s", device.name, err)

    # Fallback: derive sources from all discovered connectable advertisements.
    try:
        if discovered_service_info := getattr(bluetooth, "async_discovered_service_info", None):
            for info in discovered_service_info(device.hass, True):
                if source := getattr(info, "source", None):
                    sources.add(str(source))
    except Exception as err:
        _LOGGER.debug("Could not derive bluetooth sources for %s: %s", device.name, err)

    return sorted(sources)


def ble_device_from_source(device: ZendureDevice, mac: str, source: str) -> Any | None:
    """Return a BLEDevice for an address constrained to a specific scanner source."""
    if scanner_devices_by_address := getattr(bluetooth, "async_scanner_devices_by_address", None):
        try:
            for scanner_device in scanner_devices_by_address(device.hass, mac, True):
                if _scanner_source(scanner_device) != source:
                    continue
                if ble_device := _scanner_ble_device(scanner_device):
                    return ble_device
        except Exception as err:
            _LOGGER.debug("Could not get BLE device for %s on source %s: %s", device.name, source, err)

    return None


def ble_adapter_options(device: ZendureDevice) -> dict[int, str]:
    """Build selectable BLE adapter/source options for this device."""
    options = {0: "auto"}
    for idx, source in enumerate(ble_sources(device), start=1):
        options[idx] = source
    return options


def selected_ble_source(device: ZendureDevice) -> str | None:
    """Return configured BLE source for this device or None for auto selection."""
    if device.bleAdapter is None:
        return None

    device.bleAdapter.setDict(ble_adapter_options(device))
    source = device.bleAdapter.current_option
    return None if source in (None, "", "auto") else str(source)


async def ble_mqtt(device: ZendureDevice, mqtt: mqtt_client.Client) -> bool:
    """Set the MQTT server for the device via BLE."""
    from .api import Api

    msg: str | None = None
    try:
        if Api.wifipsw == "" or Api.wifissid == "":
            msg = "No WiFi credentials or connections found"
            return False

        mac = ble_mac(device)
        if mac is None:
            msg = "No BLE MAC address available"
            return False

        # get the bluetooth device
        ble_source = selected_ble_source(device)
        ble_device = None
        if ble_source is not None:
            ble_device = ble_device_from_source(device, mac, ble_source)

        if ble_device is None:
            ble_device = bluetooth.async_ble_device_from_address(device.hass, mac, True)

        if ble_device is None:
            msg = f"BLE device {mac} not found"
            if ble_source is not None:
                msg += f" on source {ble_source}"
            return False

        try:
            _LOGGER.info("Set mqtt %s to %s", device.name, mqtt.host)
            if establish_connection is not None:
                client = await establish_connection(BleakClient, ble_device, device.name)
            else:
                client = BleakClient(ble_device)
                await client.connect()

            try:
                await ble_command(
                    device,
                    client,
                    {
                        "iotUrl": mqtt.host,
                        "messageId": 1002,
                        "method": "token",
                        "password": Api.wifipsw,
                        "ssid": Api.wifissid,
                        "timeZone": "GMT+01:00",
                        "token": "abcdefgh",
                    },
                )

                await ble_command(
                    device,
                    client,
                    {
                        "messageId": 1003,
                        "method": "station",
                    },
                )
            finally:
                if client.is_connected:
                    await client.disconnect()
        except TimeoutError:
            msg = "Timeout when trying to connect to the BLE device"
            _LOGGER.warning(msg)
        except (AttributeError, BleakError) as err:
            msg = f"Could not connect to {device.name}: {err}"
            _LOGGER.warning(msg)
        except Exception as err:
            msg = f"BLE error: {err}"
            _LOGGER.warning(msg)
        else:
            device.mqtt = mqtt
            if device.zendure is not None:
                device.zendure.loop_stop()
                device.zendure.disconnect()
                device.zendure = None

            device.mqttPublish(device.topic_read, {"properties": ["getAll"]}, device.mqtt)
            device.setStatus()

            return True
        return False

    finally:
        if msg is not None:
            msg = f"Error setting the MQTT server on {device.name} to {mqtt.host}, {msg}"
        else:
            msg = f"Changing the MQTT server on {device.name} to {mqtt.host} was successful"

        persistent_notification.async_create(device.hass, (msg), "Zendure", "zendure_ha")

        _LOGGER.info("BLE update ready")


async def ble_command(device: ZendureDevice, client: BleakClient, command: Any) -> None:
    """Send a command to the device via BLE."""
    try:
        device._messageid += 1
        payload = json.dumps(command, default=lambda o: o.__dict__)
        b = bytearray()
        b.extend(map(ord, payload))
        _LOGGER.info("BLE command: %s => %s", device.name, payload)
        await client.write_gatt_char(SF_COMMAND_CHAR, b, response=False)
    except Exception as err:
        _LOGGER.warning("BLE error: %s", err)


def discover_ble_mac(device: ZendureDevice, si: Any) -> bool:
    """Discover and assign BLE MAC from a Bluetooth advertisement."""
    for d in si.manufacturer_data.values():
        try:
            if d is None or len(d) <= 1:
                continue
            sn = d.decode("utf8")[:-1]
            if device.snNumber.endswith(sn):
                _LOGGER.info("Found Zendure Bluetooth device: %s", si)
                device.attr_device_info["connections"] = {("bluetooth", str(si.address))}
                return True
        except Exception:
            continue
    return False
