"""Zendure Integration manager using DataUpdateCoordinator."""

from __future__ import annotations

import json
import logging
import traceback
from base64 import b64decode
from datetime import datetime, timedelta
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from homeassistant.components import bluetooth
from homeassistant.components.number import NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntry, DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from paho.mqtt import client as mqtt_client

from .api import Api
from .const import (
    CONF_MQTTLOCAL,
    CONF_MQTTLOG,
    CONF_MQTTPSW,
    CONF_MQTTSERVER,
    CONF_MQTTUSER,
    CONF_P1METER,
    CONF_WIFIPSW,
    CONF_WIFISSID,
    DOMAIN,
    ManagerState,
    SmartMode,
)
from .devices.ace1500 import ACE1500
from .devices.aio2400 import AIO2400
from .devices.hub1200 import Hub1200
from .devices.hub2000 import Hub2000
from .devices.hyper2000 import Hyper2000
from .devices.solarflow800 import SolarFlow800
from .devices.solarflow2400ac import SolarFlow2400AC
from .number import ZendureNumber, ZendureRestoreNumber
from .select import ZendureRestoreSelect, ZendureSelect
from .zenduredevice import ZendureDevice, ZendureDeviceDefinition

_LOGGER = logging.getLogger(__name__)

ZENDURE_MANAGER_STORAGE_VERSION = 1
ZENDURE_DEVICES = "devices"


class ZendureManager(DataUpdateCoordinator[int]):
    """The Zendure manager."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize ZendureManager."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({config_entry.unique_id})",
            update_interval=timedelta(seconds=90),
            always_update=True,
        )

        self._hass = hass
        self._mqtt: mqtt_client.Client | None = None
        self.p1meter = config_entry.data.get(CONF_P1METER)
        self._attr_device_info = self.attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "ZendureManager")},
            model="Zendure Manager",
            name="Zendure Manager",
            manufacturer="Fireson",
        )

        # get the local settings
        self.mqttserver: str = config_entry.data.get(CONF_MQTTSERVER, None)
        self.mqttuser: str = config_entry.data.get(CONF_MQTTUSER, None)
        self.mqttpsw: str = config_entry.data.get(CONF_MQTTPSW, None)
        self.wifissid: str = config_entry.data.get(CONF_WIFISSID, None)
        self.wifipsw: str = config_entry.data.get(CONF_WIFIPSW, None)
        self.mqttlocal = config_entry.data.get(CONF_MQTTLOCAL, False) and self.mqttserver and self.mqttuser and self.mqttpsw and self.wifissid and self.wifipsw
        ZendureDevice.logMqtt = config_entry.data.get(CONF_MQTTLOG, False)

        self.operation = 0
        self.setpoint = 0
        self.zero_idle = datetime.max
        self.zero_next = datetime.min
        self.zero_fast = datetime.min
        self.active: list[ZendureDevice] = []
        self.store: Store | None = None
        self.next_scan = datetime.now() + timedelta(seconds=300)

        # Create the api
        self.api = Api(self._hass, dict(config_entry.data))

        if "items" in config_entry.data:
            _LOGGER.info(f"Runtime data: {config_entry.as_dict()['items']}")

    async def initialize(self) -> bool:
        """Initialize the manager."""
        try:
            if not await self.api.connect():
                _LOGGER.error("Unable to connect to Zendure API")
                return False

            # load configuration from storage
            self.store = Store(self.hass, ZENDURE_MANAGER_STORAGE_VERSION, f"{DOMAIN}.storage")
            definitions = dict[str, ZendureDeviceDefinition]()
            if (storage := await self.store.async_load()) and isinstance(storage, dict):
                definitions = storage.get(ZENDURE_DEVICES, {})

            # load the devices from the api & add them to the storage
            api_devices = await self.api.getDevices()
            for key, definition in api_devices.items():
                definitions[key] = definition
            await self.store.async_save({ZENDURE_DEVICES: definitions})

            # create the devices
            await self.createDevices(definitions)
            _LOGGER.info(f"Found: {len(ZendureDevice.devicedict)} devices")

            # initialize the devices
            for device in ZendureDevice.devices:
                device.sensorsCreate()

            # Add ZendureManager sensors
            _LOGGER.info(f"Adding sensors {self.name}")
            selects = [
                ZendureRestoreSelect(
                    self._attr_device_info,
                    "Operation",
                    {0: "off", 1: "manual", 2: "smart"},
                    self.update_operation,
                    0,
                ),
            ]
            ZendureSelect.addSelects(selects)

            numbers = [
                ZendureRestoreNumber(
                    self.attr_device_info,
                    "manual_power",
                    self._update_manual_energy,
                    None,
                    "W",
                    "power",
                    10000,
                    -10000,
                    NumberMode.BOX,
                ),
            ]
            ZendureNumber.addNumbers(numbers)

            # Set sensors from values entered in config flow setup
            if self.p1meter:
                _LOGGER.info(f"Energy sensors: {self.p1meter} to _update_smart_energyp1")
                async_track_state_change_event(self._hass, [self.p1meter], self._update_smart_energyp1)

            # create the zendure cloud mqtt client
            _LOGGER.info("Create mqtt client")
            self._mqtt = mqtt_client.Client(client_id=self.api.token, clean_session=False, userdata=True)
            self._mqtt.username_pw_set(username="zenApp", password=b64decode(self.api.mqttinfo.encode()).decode("latin-1"))
            self._mqtt.on_connect = self.mqttConnect
            self._mqtt.on_disconnect = self.mqttDisconnect
            self._mqtt.on_message = self.mqttMessage
            self._mqtt.connect(self.api.mqttUrl, 1883)
            self._mqtt.suppress_exceptions = True
            self._mqtt.loop_start()
            _LOGGER.info("Zendure Manager initialized")

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())
            return False
        return True

    async def createDevices(self, definitions: dict[str, ZendureDeviceDefinition]) -> None:
        # Create the devices
        for deviceKey, deviceDef in definitions.items():
            try:
                match deviceDef.productName.lower():
                    case "hyper 2000":
                        device = Hyper2000(self._hass, deviceKey, deviceDef)
                    case "solarflow 800":
                        device = SolarFlow800(self._hass, deviceKey, deviceDef)
                    case "solarflow2.0":
                        device = Hub1200(self._hass, deviceKey, deviceDef)
                    case "solarflow hub 2000":
                        device = Hub2000(self._hass, deviceKey, deviceDef)
                    case "solarflow aio zy":
                        device = AIO2400(self._hass, deviceKey, deviceDef)
                    case "ace 1500":
                        device = ACE1500(self._hass, deviceKey, deviceDef)
                    case "solarflow 2400 ac":
                        device = SolarFlow2400AC(self._hass, deviceKey, deviceDef)
                    case _:
                        _LOGGER.info(f"Device {deviceDef.productName} is not supported!")
                        continue

                ZendureDevice.devicedict[deviceKey] = device
            except Exception as err:
                _LOGGER.error(err)
                _LOGGER.error(traceback.format_exc())

    async def unload(self) -> None:
        """Unload the manager."""
        if self._mqtt:
            for device in ZendureDevice.devices:
                self._mqtt.unsubscribe(f"/{device.prodkey}/{device.hid}/#")
                self._mqtt.unsubscribe(f"iot/{device.prodkey}/{device.hid}/#")
            self._mqtt.loop_stop()
            self._mqtt.disconnect()

        ZendureDevice.devicedict.clear()
        ZendureDevice.devices.clear()
        ZendureDevice.clusters.clear()

    async def remove_device(self, device_entry: DeviceEntry) -> None:
        """Remove a device from the manager."""
        for device in ZendureDevice.devices:
            if device.name == device_entry.name:
                _LOGGER.info(f"Remove device: {device_entry} => {device}")
                if self._mqtt:
                    self._mqtt.unsubscribe(f"/{device.prodkey}/{device.hid}/#")
                    self._mqtt.unsubscribe(f"iot/{device.prodkey}/{device.hid}/#")

                ZendureDevice.devicedict.pop(device.hid, None)
                ZendureDevice.devices.remove(device)

                # remove the device from the storage
                if self.store:
                    storage = await self.store.async_load()
                    if storage and isinstance(storage, dict):
                        devices = storage.get(ZENDURE_DEVICES, {})
                        if devices and device.hid in devices:
                            del devices[device.hid]
                            await self.store.async_save({ZENDURE_DEVICES: devices})
                return

    def update_operation(self, operation: int) -> None:
        _LOGGER.info(f"Update operation: {operation} from: {self.operation}")

        if operation == self.operation:
            return

        self.operation = operation
        if self.operation != SmartMode.MATCHING:
            for d in ZendureDevice.devices:
                d.writePower(0, self.operation == SmartMode.MANUAL)

        # One device always has it's own phase
        if len(ZendureDevice.devices) == 1 and not ZendureDevice.devices[0].clusterdevices:
            ZendureDevice.devices[0].clusterType = 1
            ZendureDevice.devices[0].clusterdevices = [ZendureDevice.devices[0]]
            ZendureDevice.clusters = [ZendureDevice.devices[0]]

    async def _async_update_data(self) -> int:
        """Refresh the data of all devices's."""
        _LOGGER.info("refresh devices")
        try:
            doscan = False
            doReset = datetime.now() - timedelta(seconds=200)
            for d in ZendureDevice.devices:
                d.sendRefresh()
                if d.bleDevice is None:
                    doscan = True
                elif d.lastUpdate < doReset:
                    await d.bleMqttReset(self.mqttserver, self.wifissid, self.wifipsw)

            if self.mqttlocal and doscan and self.next_scan < datetime.now():
                self.next_scan = datetime.now() + timedelta(seconds=300)

                async def _device_detected(device: BLEDevice, data: AdvertisementData) -> None:
                    """Handle a detected device."""
                    try:
                        if data.local_name and data.local_name.startswith("Zen") and (bts := data.manufacturer_data.get(17733, None)):
                            sn = bts.decode("utf8")[:-1]
                            _LOGGER.info(f"Found Zendure BLE device: {device.name} => {sn}")
                            if sn and (zd := next((d for d in ZendureDevice.devices if d.serial_number.endswith(sn)), None)):
                                _LOGGER.info(f"Found bluetooth for {zd.name}")
                                zd.bleDevice = device
                            else:
                                _LOGGER.info(f"Nothing Found bluetooth for {id}")
                    except Exception as err:
                        _LOGGER.error(err)
                        _LOGGER.error(traceback.format_exc())

                scanner = bluetooth.async_get_scanner(self._hass)
                scanner.register_detection_callback(_device_detected)
                await scanner.discover(timeout=10)

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())

        if self.hass and self.hass.loop.is_running():
            self._schedule_refresh()
        return 0

    def mqttConnect(self, _client: Any, _userdata: Any, _flags: Any, rc: Any) -> None:
        _LOGGER.info(f"Client has been connected, return code: {rc}")
        if rc == 0 and self._mqtt:
            for device in ZendureDevice.devices:
                device.initMqtt(self._mqtt)

    def mqttDisconnect(self, _client: Any, _userdata: Any, rc: Any) -> None:
        _LOGGER.info(f"Client disconnected from MQTT broker with return code {rc}")

    def mqttMessage(self, _client: Any, _userdata: Any, msg: Any) -> None:
        try:
            # check for valid device in payload
            payload = json.loads(msg.payload.decode())
            if (deviceid := payload.get("deviceId", None)) and (device := ZendureDevice.devicedict.get(deviceid, None)):
                device.message(msg.topic, payload)

        except:  # noqa: E722
            return

    @callback
    def _update_manual_energy(self, _number: Any, power: float) -> None:
        try:
            if self.operation == SmartMode.MANUAL:
                self.setpoint = int(power)
                self.updateSetpoint(self.setpoint, ManagerState.DISCHARGING if power >= 0 else ManagerState.CHARGING)

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())

    @callback
    def _update_smart_energyp1(self, event: Event[EventStateChangedData]) -> None:
        try:
            # exit if there is nothing to do
            if (new_state := event.data["new_state"]) is None or self.operation == SmartMode.NONE:
                return

            # convert the state to a float
            try:
                p1 = int(float(new_state.state))
            except ValueError:
                return

            # check minimal time between updates
            time = datetime.now()
            if time < self.zero_next or (time < self.zero_fast and abs(p1) < SmartMode.FAST_UPDATE):
                return

            # get the current power, exit if a device is waiting
            powerActual = 0
            for d in ZendureDevice.devices:
                d.powerAct = d.asInt("packInputPower") - (d.asInt("outputPackPower") - d.asInt("solarInputPower"))
                powerActual += d.powerAct

            _LOGGER.info(f"Update p1: {p1} power: {powerActual} operation: {self.operation}")
            # update the manual setpoint
            if self.operation == SmartMode.MANUAL:
                self.updateSetpoint(self.setpoint, ManagerState.DISCHARGING if self.setpoint >= 0 else ManagerState.CHARGING)

            # update when we are charging
            elif powerActual < 0:
                self.updateSetpoint(min(0, powerActual + p1), ManagerState.CHARGING)

            # update when we are discharging
            elif powerActual > 0:
                self.updateSetpoint(max(0, powerActual + p1), ManagerState.DISCHARGING)

            # check if it is the first time we are idle
            elif self.zero_idle == datetime.max:
                _LOGGER.info(f"Wait 10 sec for state change p1: {p1}")
                self.zero_idle = time + timedelta(seconds=SmartMode.TIMEIDLE)

            # update when we are idle for more than SmartMode.TIMEIDLE seconds
            elif self.zero_idle < time:
                if p1 < SmartMode.MIN_POWER:
                    _LOGGER.info(f"Start charging with p1: {p1}")
                    self.updateSetpoint(p1, ManagerState.CHARGING)
                    self.zero_idle = datetime.max
                elif p1 >= 0:
                    _LOGGER.info(f"Start discharging with p1: {p1}")
                    self.updateSetpoint(p1, ManagerState.DISCHARGING)
                    self.zero_idle = datetime.max
                else:
                    _LOGGER.info(f"Unable to charge/discharge p1: {p1}")

            self.zero_next = time + timedelta(seconds=SmartMode.TIMEZERO)
            self.zero_fast = time + timedelta(seconds=SmartMode.TIMEFAST)

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())

    def updateSetpoint(self, power: int, state: ManagerState) -> None:
        """Update the setpoint for all devices."""
        totalCapacity = 0
        totalPower = 0
        for d in ZendureDevice.devices:
            if state == ManagerState.DISCHARGING:
                d.capacity = max(0, d.asInt("deviceCapacity") * (d.asInt("electricLevel") - d.asInt("minSoc")))
                _LOGGER.info(f"Update capacity: {d.name} {d.capacity} = {d.asInt('deviceCapacity')} * ({d.asInt('electricLevel')} - {d.asInt('minSoc')})")
                totalPower += d.powerMax
            else:
                d.capacity = max(0, d.asInt("deviceCapacity") * (d.asInt("socSet") - d.asInt("electricLevel")))
                _LOGGER.info(f"Update capacity: {d.name} {d.capacity} = {d.asInt('deviceCapacity')} * ({d.asInt('socSet')} - {d.asInt('electricLevel')})")
                totalPower += abs(d.powerMin)
            if d.clusterType == 0:
                d.capacity = 0
            totalCapacity += d.capacity

        _LOGGER.info(f"Update setpoint: {power} state{state} capacity: {totalCapacity} max: {totalPower}")

        # redistribute the power on clusters
        isreverse = bool(abs(power) > totalPower / 2)
        active = sorted(ZendureDevice.clusters, key=lambda d: d.clustercapacity, reverse=isreverse)
        for c in active:
            clusterCapacity = c.clustercapacity
            clusterPower = int(power * clusterCapacity / totalCapacity) if totalCapacity > 0 else 0
            clusterPower = max(0, min(c.clusterMax, clusterPower)) if state == ManagerState.DISCHARGING else min(0, max(c.clusterMin, clusterPower))
            totalCapacity -= clusterCapacity

            if totalCapacity == 0:
                clusterPower = max(0, min(c.clusterMax, power)) if state == ManagerState.DISCHARGING else min(0, max(c.clusterMin, power))
            elif abs(clusterPower) > 0 and (abs(clusterPower) < SmartMode.MIN_POWER or (abs(clusterPower) < SmartMode.START_POWER and c.powerAct == 0)):
                clusterPower = 0

            for d in sorted(c.clusterdevices, key=lambda d: d.capacity, reverse=isreverse):
                if d.capacity == 0:
                    continue
                pwr = int(clusterPower * d.capacity / clusterCapacity) if clusterCapacity > 0 else 0
                clusterCapacity -= d.capacity
                pwr = max(0, min(d.powerMax, pwr)) if state == ManagerState.DISCHARGING else min(0, max(d.powerMin, pwr))
                if abs(pwr) > 0:
                    if clusterCapacity == 0:
                        pwr = max(0, min(d.powerMax, clusterPower)) if state == ManagerState.DISCHARGING else min(0, max(d.powerMin, clusterPower))
                    elif abs(pwr) > SmartMode.START_POWER or (abs(pwr) > SmartMode.MIN_POWER and d.powerAct != 0):
                        clusterPower -= pwr
                    else:
                        pwr = 0
                power -= pwr

                # update the device
                _LOGGER.info(f"Update power: {d.name} {pwr}")
                d.writePower(pwr, True)
