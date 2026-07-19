"""Zendure Integration device."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic
from typing import Any

from aiohttp import ClientTimeout
from bleak import BleakClient
from bleak.exc import BleakError

try:
    from bleak_retry_connector import establish_connection
except ImportError:
    establish_connection = None

from homeassistant.components import bluetooth, persistent_notification
from homeassistant.components.number import NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from paho.mqtt import client as mqtt_client

from .binary_sensor import ZendureBinarySensor
from .button import ZendureButton
from .const import DeviceState, SmartMode
from .entity import EntityDevice, EntityZendure
from .number import ZendureNumber
from .select import ZendureRestoreSelect, ZendureSelect
from .sensor import ZendureRestoreSensor, ZendureSensor

_LOGGER = logging.getLogger(__name__)

CONST_HEADER = {"content-type": "application/json; charset=UTF-8"}
CONST_TIMEOUT = ClientTimeout(total=4)
SF_COMMAND_CHAR = "0000c304-0000-1000-8000-00805f9b34fb"

# A SolarFlow may acknowledge zero limits and re-enable its power stage a few
# seconds later while settling an AC-mode transition.  Verify the physical
# result, not only the command registers, but keep retries bounded to avoid
# continuously controlling a device after the manager has been switched off.
POWER_OFF_VERIFY_DELAYS = (5.0, 10.0, 20.0)
POWER_OFF_TOLERANCE = 20
POWER_OFF_COALESCE = 1.0
HTTP_ERROR_STATUS = 400
POWER_CONTROL_PROPERTIES = frozenset(
    {"smartMode", "acMode", "outputLimit", "inputLimit"}
)


class ZendureBattery(EntityDevice):
    """Zendure Battery class for devices."""

    @staticmethod
    def get_battery_type(sn: str) -> tuple[str, str, float]:
        model = "???"
        match sn[0]:
            case "A":
                if sn[3] == "3":
                    model = "AIO2400"
                    kWh = 2.4
                else:
                    model = "AB1000"
                    kWh = 0.96
            case "B":
                model = "AB1000S"
                kWh = 0.96
            case "C":
                # External AB2000X and internal AB2000X of SF800+/SF800Pro/SF1600AC+ starting with CO4A. They are also described as additional battery in the Zendure App, even when they are integrated into the device.
                model = "AB2000" + ("S" if sn[3] == "F" else "X" if sn[3] == "E" else "")
                kWh = 1.92
            case "F":
                model = "AB3000"
                kWh = 2.88
            case "G":
                model = "AB3000L"
                kWh = 2.88
            case "J":
                # JO2A => internal battery of SF2400AC pro
                # JO4A => internal battery of SF2400AC+
                model = "I2400"
                kWh = 2.4
            case _:
                model = "Unknown"
                kWh = 0.0

        name = f"{model} {sn[-5:]}".strip()
        return name, model, kWh

    def __init__(self, hass: HomeAssistant, sn: str, parent: EntityDevice) -> None:
        """Initialize Device."""
        name, model, self.kWh = ZendureBattery.get_battery_type(sn)
        super().__init__(hass, sn, name, model, "", sn, parent.sn)
        self.attr_device_info["serial_number"] = sn
        self.deltaVoltage = ZendureSensor(self, "deltaVoltage", None, "V", "voltage", "measurement", 3)

    def entityUpdate(self, key: Any, value: Any) -> bool:
        """Update entity state and recalculate deltaVoltage when maxVol or minVol changes."""
        changed = super().entityUpdate(key, value)
        if changed and key in {"maxVol", "minVol"}:
            max_vol = self.entities.get("maxVol")
            min_vol = self.entities.get("minVol")
            if max_vol is not None and min_vol is not None:
                max_val = max_vol.asNumber
                min_val = min_vol.asNumber
                if max_val != 0 and min_val != 0:
                    self.deltaVoltage.update_value(round(max_val - min_val, 3))
        return changed


class ZendureDevice(EntityDevice):
    """Zendure Device class for devices integration."""

    def __init__(self, hass: HomeAssistant, deviceId: str, name: str, model: str, definition: dict[str, str], parent: str | None = None) -> None:
        """Initialize Device."""
        from .fusegroup import FuseGroup

        """Initialize Device."""
        self.prodkey = definition["productKey"]
        super().__init__(hass, deviceId, name, model, self.prodkey, definition["snNumber"], parent)
        self.snNumber = definition["snNumber"]
        self.definition = definition
        self.fuseGrp: FuseGroup

        self.mqtt: mqtt_client.Client | None = None
        self.zendure: mqtt_client.Client | None = None
        self.ipAddress = definition.get("ip", "") if definition.get("ip", "") != "" else f"zendure-{definition['productModel'].replace(' ', '')}-{self.snNumber}.local"

        self.topic_read = f"iot/{self.prodkey}/{self.deviceId}/properties/read"
        self.topic_write = f"iot/{self.prodkey}/{self.deviceId}/properties/write"
        self.topic_function = f"iot/{self.prodkey}/{self.deviceId}/function/invoke"

        self.batteries: dict[str, ZendureBattery | None] = {}
        self.lastseen = datetime.min
        self._messageid = 0
        self.kWh = 0.0

        self.charge_limit: int = 0
        self.charge_optimal: int = 0
        self.charge_start: int = 0
        self.discharge_limit: int = 0
        self.discharge_optimal: int = 0
        self.discharge_start: int = 0
        self.maxSolar = 0
        self.pwr_max: int = 0
        self.pwr_produced: int = 0
        self.actualKwh: float = 0.0
        self.state: DeviceState = DeviceState.OFFLINE
        self.exports_bypass: bool = True

        self.create_entities()

    def create_entities(self) -> None:
        """Create the device entities."""
        self.limitOutput = ZendureNumber(self, "outputLimit", self.entityWrite, None, "W", "power", self.discharge_limit, 0, NumberMode.SLIDER)
        self.limitInput = ZendureNumber(self, "inputLimit", self.entityWrite, None, "W", "power", self.charge_limit, 0, NumberMode.SLIDER)
        self.minSoc = ZendureNumber(self, "minSoc", self.entityWrite, None, "%", "soc", 100, 0, NumberMode.SLIDER, 10)
        self.socSet = ZendureNumber(self, "socSet", self.entityWrite, None, "%", "soc", 100, 0, NumberMode.SLIDER, 10)
        self.socStatus = ZendureSensor(self, "socStatus", state=0)
        self.socLimit = ZendureSensor(self, "socLimit", state=0)
        self.byPass = ZendureSensor(self, "pass", state=0)

        fuseGroups = {0: "unused", 1: "owncircuit", 2: "group800", 3: "group800_2400", 4: "group1200", 5: "group2000", 6: "group2400", 7: "group3600"}
        self.fuseGroup = ZendureRestoreSelect(self, "fuseGroup", fuseGroups, None)
        self.acMode = ZendureSelect(self, "acMode", {1: "input", 2: "output"}, self.entityWrite, 1)
        self.electricLevel = ZendureSensor(self, "electricLevel", None, "%", "battery", "measurement")
        # set homeInput to 0 for devices, which have no AC charge capability
        self.homeInput = ZendureSensor(self, "gridInputPower", None, "W", "power", "measurement", state = 0)
        self.solarInput = ZendureSensor(self, "solarInputPower", None, "W", "power", "measurement", icon="mdi:solar-panel")
        self.batteryInput = ZendureSensor(self, "outputPackPower", None, "W", "power", "measurement")
        self.batteryOutput = ZendureSensor(self, "packInputPower", None, "W", "power", "measurement")
        self.homeOutput = ZendureSensor(self, "outputHomePower", None, "W", "power", "measurement")
        self.batInOut = ZendureSensor(self, "batInOut", None, "W", "power", "measurement", 0)
        self.heatState = ZendureBinarySensor(self, "heatState")
        self.hemsState = ZendureBinarySensor(self, "hemsState")
        self.hemsStateUpdated = datetime.min
        self.availableKwh = ZendureSensor(self, "available_kwh", None, "kWh", "energy_storage", None, 1)
        self.totalKwh = ZendureSensor(self, "total_kwh", None, "kWh", "energy_storage", "measurement", 2)
        self.connectionStatus = ZendureSensor(self, "connectionStatus")
        self.connection: ZendureRestoreSelect
        self.bleAdapter: ZendureRestoreSelect | None = None
        self.remainingTime = ZendureSensor(self, "remainingTime", None, "h", "duration", "measurement")
        self.nextCalibration = ZendureRestoreSensor(self, "nextCalibration", None, None, "timestamp", None)

        self.aggrCharge = ZendureRestoreSensor(self, "aggrCharge", None, "kWh", "energy", "total_increasing", 2)
        self.aggrDischarge = ZendureRestoreSensor(self, "aggrDischarge", None, "kWh", "energy", "total_increasing", 2)
        # Round-trip efficiency: ratio of total energy discharged to total energy charged, expressed as a percentage
        self.roundtripEfficiency = ZendureSensor(self, "roundtripEfficiency", None, "%", None, "measurement", 1)
        self.aggrHomeInput = ZendureRestoreSensor(self, "aggrGridInputPower", None, "kWh", "energy", "total_increasing", 2)
        self.aggrHomeOut = ZendureRestoreSensor(self, "aggrOutputHome", None, "kWh", "energy", "total_increasing", 2)
        self.aggrSolar = ZendureRestoreSensor(self, "aggrSolar", None, "kWh", "energy", "total_increasing", 2)
        self.aggrSwitchCount = ZendureRestoreSensor(self, "switchCount", None, None, None, "total_increasing", 0)

    def setLimits(self, charge: int, discharge: int) -> None:
        """Set the device limits."""
        try:
            self.charge_limit = charge
            self.charge_optimal = charge // 4
            self.charge_start = charge // 10
            self.limitInput.update_range(0, abs(charge))

            self.discharge_limit = discharge
            self.discharge_optimal = discharge // 4
            self.discharge_start = discharge // 10
            self.limitOutput.update_range(0, discharge)
        except Exception:
            _LOGGER.error("SetLimits error %s %s %s!", self.name, charge, discharge)

    def setStatus(self) -> None:
        from .api import Api

        try:
            if self.lastseen == datetime.min:
                self.connectionStatus.update_value(0)
            elif self.socStatus.asInt == 1:
                self.connectionStatus.update_value(1)
            elif self.hemsState.is_on:
                self.connectionStatus.update_value(2)
            elif self.fuseGroup.value == 0:
                self.connectionStatus.update_value(3)
            elif self.connection.value == SmartMode.ZENSDK:
                self.connectionStatus.update_value(12)
            elif self.mqtt is not None and self.mqtt.host == Api.localServer:
                self.connectionStatus.update_value(11)
            else:
                self.connectionStatus.update_value(10)
        except Exception:
            self.connectionStatus.update_value(0)

    def entityUpdate(self, key: Any, value: Any) -> bool:
        # update entity state
        if key in {"remainOutTime", "remainInputTime"}:
            self.remainingTime.update_value(self.calcRemainingTime())
            return True

        changed = super().entityUpdate(key, value)
        try:
            if changed:
                match key:
                    case "packState":
                        if value == 0:
                            self.aggrSwitchCount.update_value(1 + self.aggrSwitchCount.asNumber)
                    case "outputPackPower":
                        if not self.heatState.is_on:
                            self.aggrCharge.aggregate(dt_util.now(), value)
                        self.aggrDischarge.aggregate(dt_util.now(), 0)
                        self.batInOut.update_value(self.batteryOutput.asInt - self.batteryInput.asInt)
                        self.roundtripEfficiency.update_value(round(self.aggrDischarge.asNumber / charge * 100, 1) if (charge := self.aggrCharge.asNumber) > 0 else 0)
                    case "packInputPower":
                        self.aggrCharge.aggregate(dt_util.now(), 0)
                        self.aggrDischarge.aggregate(dt_util.now(), value)
                        self.batInOut.update_value(self.batteryOutput.asInt - self.batteryInput.asInt)
                        self.roundtripEfficiency.update_value(round(self.aggrDischarge.asNumber / charge * 100, 1) if (charge := self.aggrCharge.asNumber) > 0 else 0)
                    case "solarInputPower":
                        self.aggrSolar.aggregate(dt_util.now(), value)
                    case "gridInputPower":
                        self.aggrHomeInput.aggregate(dt_util.now(), value)
                    case "outputHomePower":
                        self.aggrHomeOut.aggregate(dt_util.now(), value)
                    case "gridOffPower":
                        self.aggrOffGrid.aggregate(dt_util.now(), value)
                    case "inverseMaxPower":
                        self.setLimits(self.charge_limit, value)
                    case "chargeLimit" | "chargeMaxLimit":
                        self.setLimits(-value, self.discharge_limit)
                    case "hemsState" | "socStatus":
                        self.setStatus()
                        if key == "socStatus" and self.socStatus.asInt == 0:
                            self.nextCalibration.update_value(dt_util.now() + timedelta(days=30))
                    case "electricLevel" | "minSoc" | "socLimit":
                        if self.electricLevel.asInt == 100:
                            self.nextCalibration.update_value(dt_util.now() + timedelta(days=30))
                        self.availableKwh.update_value((self.electricLevel.asNumber - self.minSoc.asNumber) / 100 * self.kWh)
                    case "gridReverse":
                        self.exports_bypass = value == 1
        except Exception as e:
            _LOGGER.error("EntityUpdate error %s %s %s!", self.name, key, e)
            _LOGGER.error(traceback.format_exc())

        return changed

    def calcRemainingTime(self) -> float:
        """Calculate the remaining time."""
        level = self.electricLevel.asInt
        power = self.batteryOutput.asInt - self.batteryInput.asInt

        if power == 0:
            return 0

        if power < 0:
            soc = self.socSet.asNumber
            return 0 if level >= soc else min(999, self.kWh * 10 / -power * (soc - level))

        soc = self.minSoc.asNumber
        return 0 if level <= soc else min(999, self.kWh * 10 / power * (level - soc))

    async def entityWrite(self, entity: EntityZendure, value: Any) -> None:
        if entity.translation_key is None:
            _LOGGER.error("Entity %s has no translation_key, cannot write property %s", entity.name, self.name)
            return

        _LOGGER.info("Writing property %s %s => %s", self.name, entity.propertyName, value)
        self._messageid += 1
        payload = json.dumps(
            {
                "deviceId": self.deviceId,
                "messageId": self._messageid,
                "timestamp": int(datetime.now().timestamp()),
                "properties": {entity.propertyName: value},
            },
            default=lambda o: o.__dict__,
        )
        if self.mqtt is not None:
            self.mqtt.publish(self.topic_write, payload)

    async def button_press(self, _key: str) -> None:
        return

    def mqttPublish(self, topic: str, command: Any, client: mqtt_client.Client | None = None) -> None:
        command["messageId"] = self._messageid
        command["deviceId"] = self.deviceId
        command["timestamp"] = int(datetime.now().timestamp())
        payload = json.dumps(command, default=lambda o: o.__dict__)

        if client is not None:
            client.publish(topic, payload)
        elif self.mqtt is not None:
            self.mqtt.publish(topic, payload)

    def mqttInvoke(self, command: Any) -> None:
        self._messageid += 1
        command["messageId"] = self._messageid
        command["deviceKey"] = self.deviceId
        command["timestamp"] = int(datetime.now().timestamp())
        self.mqttPublish(self.topic_function, command)

    async def mqttProperties(self, payload: Any) -> None:
        if self.lastseen == datetime.min:
            self.lastseen = datetime.now() + timedelta(minutes=5)
            self.setStatus()
        else:
            self.lastseen = datetime.now() + timedelta(minutes=5)

        if (properties := payload.get("properties", None)) and len(properties) > 0:
            for key, value in properties.items():
                self.entityUpdate(key, value)

        # update the battery properties
        if batprops := payload.get("packData", None):
            for b in batprops:
                if (sn := b.get("sn", None)) is None:
                    continue

                if (bat := self.batteries.get(sn, None)) is None:
                    bat = ZendureBattery(self.hass, sn, self)
                    self.batteries[sn] = bat

                # Always apply properties — including for newly created batteries.
                # With elif, a new battery received no entityUpdate on its first packData
                # message, so HA entities were never created until the *next* poll cycle
                # (every 60 s).  This caused batteries to be invisible after a failed
                # initial httpGet (e.g. brief WiFi outage at startup).
                if bat and b:
                    for key, value in b.items():
                        if key != "sn":
                            bat.entityUpdate(key, value)

            # Recalculate total capacity after every packData update
            # (covers both new batteries and potential pack changes)
            self.kWh = sum(0 if b is None else b.kWh for b in self.batteries.values())
            self.totalKwh.update_value(self.kWh)
            self.availableKwh.update_value((self.electricLevel.asNumber - self.minSoc.asNumber) / 100 * self.kWh)

    def mqttMessage(self, topic: str, payload: Any) -> bool:
        try:
            match topic:
                case "properties/report":
                    asyncio.run_coroutine_threadsafe(self.mqttProperties(payload), self.hass.loop)
                    # self.mqttProperties(payload)

                case "register/replay":
                    _LOGGER.info("Register replay for %s => %s", self.name, payload)
                    if self.mqtt is not None:
                        self.mqtt.publish(f"iot/{self.prodkey}/{self.deviceId}/register/replay", None, 1, True)

                case "time-sync":
                    return True

                case "properties/energy":
                    self.hemsState.update_value(1)
                    self.hemsStateUpdated = datetime.now()
                    self.setStatus()
                    return True

                case "event/device" | "event/error":
                    return True

                case "properties/read" | "function/invoke/reply" | "properties/read/reply" | "config" | "log" | "function/invoke":
                    return False

                # case "firmware/report":
                #     _LOGGER.info("Firmware report for %s => %s", self.name, payload)
                case _:
                    return False
        except Exception as err:
            _LOGGER.error(err)

        return True

    async def mqttSelect(self, _select: ZendureRestoreSelect, _value: Any) -> None:
        from .api import Api

        self.mqtt = None
        if self.lastseen != datetime.min:
            if self.connection.value == 0:
                await self.bleMqtt(Api.mqttCloud)
            elif self.connection.value == 1:
                await self.bleMqtt(Api.mqttLocal)

        _LOGGER.debug("Mqtt selected %s", self.name)

    @property
    def bleMac(self) -> str | None:
        if (conn := self.attr_device_info.get("connections", None)) is not None:
            for connection_type, mac_address in conn:
                if connection_type == "bluetooth":
                    return mac_address
        return None

    @staticmethod
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

    @staticmethod
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

    def ble_sources(self) -> list[str]:
        """Get available Bluetooth source identifiers from Home Assistant."""
        sources: set[str] = set()
        ble_mac = self.bleMac

        # Prefer scanner sources for this specific device.
        try:
            if ble_mac and (scanner_devices_by_address := getattr(bluetooth, "async_scanner_devices_by_address", None)):
                for scanner_device in scanner_devices_by_address(self.hass, ble_mac, True):
                    if source := self._scanner_source(scanner_device):
                        sources.add(source)
        except Exception as err:
            _LOGGER.debug("Could not read bluetooth scanner sources for %s: %s", self.name, err)

        # Fallback: derive sources from all discovered connectable advertisements.
        try:
            if discovered_service_info := getattr(bluetooth, "async_discovered_service_info", None):
                for info in discovered_service_info(self.hass, True):
                    if source := getattr(info, "source", None):
                        sources.add(str(source))
        except Exception as err:
            _LOGGER.debug("Could not derive bluetooth sources for %s: %s", self.name, err)

        return sorted(sources)

    def ble_device_from_source(self, ble_mac: str, source: str) -> Any | None:
        """Return a BLEDevice for an address constrained to a specific scanner source."""
        if scanner_devices_by_address := getattr(bluetooth, "async_scanner_devices_by_address", None):
            try:
                for scanner_device in scanner_devices_by_address(self.hass, ble_mac, True):
                    if self._scanner_source(scanner_device) != source:
                        continue
                    if device := self._scanner_ble_device(scanner_device):
                        return device
            except Exception as err:
                _LOGGER.debug("Could not get BLE device for %s on source %s: %s", self.name, source, err)

        return None

    def ble_adapter_options(self) -> dict[int, str]:
        """Build selectable BLE adapter/source options for this device."""
        options = {0: "auto"}
        for idx, source in enumerate(self.ble_sources(), start=1):
            options[idx] = source
        return options

    def selected_ble_source(self) -> str | None:
        """Return configured BLE source for this device or None for auto selection."""
        if self.bleAdapter is None:
            return None

        self.bleAdapter.setDict(self.ble_adapter_options())
        source = self.bleAdapter.current_option
        return None if source in (None, "", "auto") else str(source)

    async def bleMqtt(self, mqtt: mqtt_client.Client) -> bool:
        """Set the MQTT server for the device via BLE."""
        from .api import Api

        msg: str | None = None
        try:
            if Api.wifipsw == "" or Api.wifissid == "":
                msg = "No WiFi credentials or connections found"
                return False

            if (ble_mac := self.bleMac) is None:
                msg = "No BLE MAC address available"
                return False

            # get the bluetooth device
            ble_source = self.selected_ble_source()
            device = None
            if ble_source is not None:
                device = self.ble_device_from_source(ble_mac, ble_source)

            if device is None:
                device = bluetooth.async_ble_device_from_address(self.hass, ble_mac, True)

            if device is None:
                msg = f"BLE device {ble_mac} not found"
                if ble_source is not None:
                    msg += f" on source {ble_source}"
                return False

            try:
                _LOGGER.info("Set mqtt %s to %s", self.name, mqtt.host)
                if establish_connection is not None:
                    client = await establish_connection(BleakClient, device, self.name)
                else:
                    client = BleakClient(device)
                    await client.connect()

                try:
                    await self.bleCommand(
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

                    await self.bleCommand(
                        client,
                        {
                            "messageId": 1003,
                            "method": "station",
                        },
                    )
                finally:
                    # Ensure stale BLE sessions do not leak if command execution fails unexpectedly.
                    if client.is_connected:
                        await client.disconnect()
            except TimeoutError:
                msg = "Timeout when trying to connect to the BLE device"
                _LOGGER.warning(msg)
            except (AttributeError, BleakError) as err:
                msg = f"Could not connect to {self.name}: {err}"
                _LOGGER.warning(msg)
            except Exception as err:
                msg = f"BLE error: {err}"
                _LOGGER.warning(msg)
            else:
                self.mqtt = mqtt
                if self.zendure is not None:
                    self.zendure.loop_stop()
                    self.zendure.disconnect()
                    self.zendure = None

                self.mqttPublish(self.topic_read, {"properties": ["getAll"]}, self.mqtt)
                self.setStatus()

                return True
            return False

        finally:
            if msg is not None:
                msg = f"Error setting the MQTT server on {self.name} to {mqtt.host}, {msg}"
            else:
                msg = f"Changing the MQTT server on {self.name} to {mqtt.host} was successful"

            persistent_notification.async_create(self.hass, (msg), "Zendure", "zendure_ha")

            _LOGGER.info("BLE update ready")

    async def bleCommand(self, client: BleakClient, command: Any) -> None:
        try:
            self._messageid += 1
            payload = json.dumps(command, default=lambda o: o.__dict__)
            b = bytearray()
            b.extend(map(ord, payload))
            _LOGGER.info("BLE command: %s => %s", self.name, payload)
            await client.write_gatt_char(SF_COMMAND_CHAR, b, response=False)
        except Exception as err:
            _LOGGER.warning("BLE error: %s", err)

    async def power_get(self) -> bool:
        if self.lastseen < datetime.now():
            self.lastseen = datetime.min
            self.setStatus()

        self.actualKwh = self.availableKwh.asNumber

        if not self.online or self.socSet.asNumber == 0 or self.kWh == 0:
            self.state = DeviceState.OFFLINE
        elif self.socLimit.asInt == SmartMode.SOCFULL or self.electricLevel.asInt >= self.socSet.asNumber:
            self.state = DeviceState.SOCFULL
        elif self.socLimit.asInt == SmartMode.SOCEMPTY or self.electricLevel.asInt <= self.minSoc.asNumber:
            self.state = DeviceState.SOCEMPTY
        else:
            self.state = DeviceState.INACTIVE

        return self.state != DeviceState.OFFLINE

    async def charge(self, _power: int) -> int:
        """Set the power output/input."""
        return 0

    async def power_charge(self, power: int) -> int:
        """Set charge power."""
        power = min(0, max(power, self.charge_limit))
        """power is here a negative value, but homeInput and homeOutput are always positive"""
        if abs(power + self.homeInput.asInt - self.homeOutput.asInt) <= SmartMode.POWER_TOLERANCE:
            _LOGGER.info("Power charge %s => no action [power %s]", self.name, power)
            return - self.homeInput.asInt
        return await self.charge(power)

    async def discharge(self, _power: int) -> int:
        """Set the power output/input."""
        return 0

    async def power_discharge(self, power: int) -> int:
        """Set discharge power."""
        power = max(0, min(power, self.discharge_limit))
        if abs(power - self.homeOutput.asInt + self.homeInput.asInt) <= SmartMode.POWER_TOLERANCE:
            _LOGGER.info("Power discharge %s => no action [power %s]", self.name, power)
            return self.homeOutput.asInt
        return await self.discharge(power)

    async def power_off(self) -> None:
        """Set the power off."""

    def cancel_pending_tasks(self) -> None:
        """Cancel device background work during integration unload."""

    @property
    def online(self) -> bool:
        """Check if device is online."""
        return self.connectionStatus.asInt >= SmartMode.CONNECTED

    @property
    def pwr_offgrid(self) -> int:
        """Get the offgrid power."""
        return 0


class ZendureLegacy(ZendureDevice):
    """Zendure Legacy class for devices."""

    def __init__(self, hass: HomeAssistant, deviceId: str, name: str, model: str, definition: dict[str, str], parent: str | None = None) -> None:
        """Initialize Device."""
        super().__init__(hass, deviceId, name, model, definition, parent)
        self.connection = ZendureRestoreSelect(self, "connection", {0: "cloud", 1: "local"}, self.mqttSelect, 0)
        self.mqttReset = ZendureButton(self, "mqttReset", self.button_press)
        self.bleAdapter = ZendureRestoreSelect(self, "bleAdapter", self.ble_adapter_options(), self.bleAdapterSelect, 0)

    async def bleAdapterSelect(self, _select: ZendureRestoreSelect, _value: Any) -> None:
        # Refresh available sources whenever selection changes or is restored.
        if self.bleAdapter is not None:
            self.bleAdapter.setDict(self.ble_adapter_options())

    async def button_press(self, button: ZendureButton) -> None:
        from .api import Api

        match button.translation_key:
            case "mqtt_reset":
                _LOGGER.info("Resetting MQTT for %s", self.name)
                await self.bleMqtt(Api.mqttCloud if self.connection.value == 0 else Api.mqttLocal)

    async def dataRefresh(self, _update_count: int) -> None:
        """Refresh the device data."""
        from .api import Api

        if self.lastseen != datetime.min:
            self.mqttPublish(self.topic_read, {"properties": ["getAll"]}, self.mqtt)
        else:
            self.mqttPublish(self.topic_read, {"properties": ["getAll"]}, Api.mqttCloud)
            self.mqttPublish(self.topic_read, {"properties": ["getAll"]}, Api.mqttLocal)

    def mqttMessage(self, topic: str, payload: Any) -> bool:
        if topic == "register/replay":
            _LOGGER.info("Register replay for %s => %s", self.name, payload)
            return True

        return super().mqttMessage(topic, payload)


class ZendureZenSdk(ZendureDevice):
    """Zendure Zen SDK class for devices."""

    def __init__(self, hass: HomeAssistant, deviceId: str, name: str, model: str, definition: dict[str, str], parent: str | None = None) -> None:
        """Initialize Device."""
        self.session = async_get_clientsession(hass, verify_ssl=False)
        super().__init__(hass, deviceId, name, model, definition, parent)
        self.connection = ZendureRestoreSelect(self, "connection", {0: "cloud", 2: "zenSDK"}, self.mqttSelect, 0)
        self.httpid = 0
        # All power-control sources (manager, HA number entities and recovery)
        # share this lock so their HTTP/MQTT commands cannot overtake each other.
        self._command_lock = asyncio.Lock()
        self._power_command_generation = 0
        self._power_off_requested = False
        self._power_off_task: asyncio.Task[None] | None = None
        self._last_power_off_command = 0.0
        self._last_power_off_properties: dict[str, int] | None = None
        self._power_off_verify_delays = POWER_OFF_VERIFY_DELAYS

    async def mqttSelect(self, select: Any, _value: Any) -> None:
        from .api import Api

        self.mqtt = None
        match select.value:
            case 0:
                Api.mqttCloud.unsubscribe(f"/{self.prodkey}/{self.deviceId}/#")
                Api.mqttCloud.unsubscribe(f"iot/{self.prodkey}/{self.deviceId}/#")

            case 2:
                Api.mqttCloud.unsubscribe(f"/{self.prodkey}/{self.deviceId}/#")
                Api.mqttCloud.unsubscribe(f"iot/{self.prodkey}/{self.deviceId}/#")

        _LOGGER.debug("Mqtt selected %s", self.name)

    async def entityWrite(self, entity: EntityZendure, value: Any) -> None:
        if entity.translation_key is None:
            _LOGGER.error("Entity %s has no translation_key, cannot write property %s", entity.name, self.name)
            return

        # Route limit writes through the power routines so they send the full command
        # (smartMode/acMode), exactly like the manager does. A bare outputLimit/inputLimit
        # property write is silently ignored when the device has dropped out of smart mode
        # (observed on SolarFlow 2400 Pro at 100% SoC, see #1505).
        if entity.propertyName == "outputLimit":
            await self.discharge(value)
        elif entity.propertyName == "inputLimit":
            await self.charge(-value)
        elif self.online and self.connection.value == 0:
            async with self._command_lock:
                if entity.propertyName in POWER_CONTROL_PROPERTIES:
                    self._invalidate_power_off_locked()
                await super().entityWrite(entity, value)
        else:
            _LOGGER.info("Writing property %s %s => %s", self.name, entity.propertyName, value)
            await self.doCommand({"properties": {entity.propertyName: value}})

    async def dataRefresh(self, update_count: int) -> None:
        if update_count == 0 and not self.online:
            json = await self.httpGet("properties/report")
            await self.mqttProperties(json)

    async def power_get(self) -> bool:
        """Get the current power."""
        if self.connection.value != 0:
            json = await self.httpGet("properties/report")
            await self.mqttProperties(json)

        return await super().power_get()

    async def charge(self, power: int, _off: bool = False) -> int:
        """Set charge power."""
        _LOGGER.info("Power charge %s => %s", self.name, power)
        # Zero is directionless.  Reusing the canonical OFF payload prevents a
        # pair of outputLimit=0/inputLimit=0 writes from ending in AC input mode.
        if power == 0:
            await self.power_off()
            return 0
        if (
            power == -SmartMode.POWER_START
            and self.limitInput.asInt >= SmartMode.POWER_START
            and self.homeInput.asInt == 0
        ):
            power = -min(self.limitInput.asInt + 4, 2 * SmartMode.POWER_START)
            _LOGGER.info("Power charge kickstart %s => %s", self.name, power)
        await self._write_power_command(
            {
                "smartMode": 1,
                "acMode": 1,
                "outputLimit": 0,
                "inputLimit": -power,
            }
        )
        return power

    async def discharge(self, power: int) -> int:
        _LOGGER.info("Power discharge %s => %s", self.name, power)
        if power == 0:
            await self.power_off()
            return 0
        if (
            power == SmartMode.POWER_START
            and self.limitOutput.asInt >= SmartMode.POWER_START
            and self.homeOutput.asInt == 0
        ):
            power = min(self.limitOutput.asInt + 4, 2 * SmartMode.POWER_START)
            _LOGGER.info("Power discharge kickstart %s => %s", self.name, power)
        await self._write_power_command(
            {
                "smartMode": 1,
                "acMode": 2,
                "outputLimit": power,
                "inputLimit": 0,
            }
        )
        return power

    async def power_off(self) -> None:
        """Set the power off."""
        properties = self._power_off_properties()
        generation = await self._write_power_command(properties, power_off=True)
        self._schedule_power_off_verification(generation)

    def _power_off_properties(self) -> dict[str, int]:
        """Return the single canonical payload used to stop either direction."""
        return {
            "smartMode": 0 if self.pwr_offgrid == 0 else 1,
            "acMode": 2,
            "outputLimit": 0,
            "inputLimit": 0,
        }

    def _cancel_power_off_verification(self) -> None:
        """Cancel a stale power-off verifier without cancelling itself."""
        task = self._power_off_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        if task is not asyncio.current_task():
            self._power_off_task = None

    def _invalidate_power_off_locked(self) -> None:
        """Record a newer power intent while the caller owns _command_lock."""
        self._power_command_generation += 1
        self._power_off_requested = False
        self._last_power_off_properties = None
        self._cancel_power_off_verification()

    async def _write_power_command(
        self,
        properties: dict[str, int],
        *,
        power_off: bool = False,
    ) -> int:
        """Serialize one power command and return its ordering generation."""
        async with self._command_lock:
            # Manager OFF followed immediately by outputLimit=0 and inputLimit=0
            # is the same intent. Preserve its generation and verifier so a
            # stream of duplicate zero writes cannot postpone verification.
            now = monotonic()
            coalesced = (
                power_off
                and self._power_off_requested
                and properties == self._last_power_off_properties
                and now - self._last_power_off_command < POWER_OFF_COALESCE
            )
            if coalesced:
                return self._power_command_generation

            self._power_command_generation += 1
            generation = self._power_command_generation
            self._power_off_requested = power_off
            if not power_off:
                self._last_power_off_properties = None
            self._cancel_power_off_verification()

            success = await self._send_command_unlocked({"properties": properties})
            if success and power_off:
                self._last_power_off_command = monotonic()
                self._last_power_off_properties = properties.copy()

        return generation

    def _schedule_power_off_verification(self, generation: int) -> None:
        """Verify an OFF transition in the background."""
        # power_off() releases the command lock before scheduling. A newer
        # command may therefore already exist; never replace its verifier with
        # a task for this stale generation.
        if not self._is_current_power_off(generation):
            return
        if self._power_off_task is not None and not self._power_off_task.done():
            return
        self._cancel_power_off_verification()
        self._power_off_task = self.hass.async_create_task(
            self._verify_power_off(generation)
        )

    def _is_power_off(self) -> bool:
        """Check measured power flows, not only requested limit registers."""
        battery_discharge, home_output, home_input = (
            self._power_off_measurements()
        )
        return (
            home_output <= POWER_OFF_TOLERANCE
            and home_input <= POWER_OFF_TOLERANCE
            and battery_discharge <= POWER_OFF_TOLERANCE
        )

    def _power_off_measurements(self) -> tuple[int, int, int]:
        """Return net managed battery, home-output and home-input power."""
        offgrid = max(0, self.pwr_offgrid)
        battery_discharge = max(
            0,
            self.batteryOutput.asInt - self.batteryInput.asInt - offgrid,
        )
        return battery_discharge, self.homeOutput.asInt, self.homeInput.asInt

    def _is_current_power_off(self, generation: int) -> bool:
        """Return whether a verifier still represents the latest power intent."""
        return (
            generation == self._power_command_generation
            and self._power_off_requested
        )

    async def _check_and_retry_power_off(self, generation: int, attempt: int) -> bool:
        """Retry active power and return True only when this verifier is stale."""
        # zenSDK obtains a fresh report here; MQTT devices use their most recent
        # telemetry. A failed refresh is not proof of a failed stop.
        if not await self.power_get():
            return False
        if self._is_power_off():
            # Do not finish after the first zero sample: the SF2400 can report a
            # successful stop and re-enable its power stage several seconds later.
            return False

        async with self._command_lock:
            if not self._is_current_power_off(generation):
                return True
            battery_discharge, home_output, home_input = (
                self._power_off_measurements()
            )
            _LOGGER.warning(
                "Power-off verification %s/%s failed for %s "
                "(battery=%sW, homeOut=%sW, homeIn=%sW); retrying",
                attempt,
                len(self._power_off_verify_delays),
                self.name,
                battery_discharge,
                home_output,
                home_input,
            )
            await self._send_command_unlocked(
                {"properties": self._power_off_properties()}
            )
        return False

    async def _report_power_off_failure(self, generation: int) -> None:
        """Refresh and report a stop that did not converge after all retries."""
        if not self._is_current_power_off(generation):
            return
        if self._power_off_verify_delays:
            await asyncio.sleep(self._power_off_verify_delays[0])
        if not await self.power_get():
            if not self._is_current_power_off(generation):
                return
            _LOGGER.warning(
                "Unable to verify power-off for offline device %s",
                self.name,
            )
            return
        if not self._is_current_power_off(generation):
            return
        if not self._is_power_off():
            battery_discharge, home_output, home_input = (
                self._power_off_measurements()
            )
            _LOGGER.error(
                "Unable to stop power flow for %s after %s retries "
                "(battery=%sW, homeOut=%sW, homeIn=%sW)",
                self.name,
                len(self._power_off_verify_delays),
                battery_discharge,
                home_output,
                home_input,
            )

    async def _verify_power_off(self, generation: int) -> None:
        """Retry a stop that was acknowledged but did not stop physical power."""
        try:
            for attempt, delay in enumerate(self._power_off_verify_delays, start=1):
                await asyncio.sleep(delay)

                if not self._is_current_power_off(generation):
                    return

                if await self._check_and_retry_power_off(generation, attempt):
                    return

            await self._report_power_off_failure(generation)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Power-off verification failed for %s", self.name)
        finally:
            if self._power_off_task is asyncio.current_task():
                self._power_off_task = None

    def cancel_pending_tasks(self) -> None:
        """Cancel device background work during integration unload."""
        self._cancel_power_off_verification()
        self._power_off_task = None

    async def doCommand(self, command: Any) -> bool:
        """Serialize a non-distribution command with power commands."""
        async with self._command_lock:
            properties = (
                command.get("properties", {}) if isinstance(command, dict) else {}
            )
            if POWER_CONTROL_PROPERTIES.intersection(properties):
                self._invalidate_power_off_locked()
            return await self._send_command_unlocked(command)

    async def _send_command_unlocked(self, command: Any) -> bool:
        """Send a command while the caller owns _command_lock."""
        if self.connection.value != 0:
            return await self.httpPost("properties/write", command)
        if self.mqtt is None:
            _LOGGER.error("Cannot send command to %s: MQTT is unavailable", self.name)
            return False
        self.mqttPublish(self.topic_write, command, self.mqtt)
        return True

    async def httpGet(self, url: str, key: str | None = None) -> dict[str, Any]:
        try:
            url = f"http://{self.ipAddress}/{url}"
            response = await self.session.get(url, headers=CONST_HEADER, timeout=CONST_TIMEOUT)
            payload = json.loads(await response.text())
            self.lastseen = datetime.now()
            return payload if key is None else payload.get(key, {})
        except Exception as e:
            _LOGGER.error("%s for %s during httpGet%s", type(e).__name__, self.name, f": {e}" if str(e) else "!")
            self.lastseen = datetime.min
        return {}

    async def httpPost(self, url: str, command: Any) -> bool:
        try:
            self.httpid += 1
            command["id"] = self.httpid
            command["sn"] = self.snNumber
            url = f"http://{self.ipAddress}/{url}"
            response = await self.session.post(
                url,
                json=command,
                headers=CONST_HEADER,
                timeout=CONST_TIMEOUT,
            )
            try:
                status = getattr(response, "status", 200)
                if status >= HTTP_ERROR_STATUS:
                    body = await response.text()
                    _LOGGER.error(
                        "HTTP %s for %s during httpPost: %s",
                        status,
                        self.name,
                        body[:200],
                    )
                    return False
            finally:
                response.release()
        except Exception as e:
            _LOGGER.error("%s for %s during httpPost%s", type(e).__name__, self.name, f": {e}" if str(e) else "!")
            self.lastseen = datetime.min
            return False
        return True


@dataclass
class DeviceSettings:
    device_id: str
    fuseGroup: str
    limitCharge: int
    limitDischarge: int
    maxSolar: int
    kWh: float = 0.0
    socSet: float = 100
    minSoc: float = 0
