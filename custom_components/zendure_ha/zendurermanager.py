"""Zendure Integration manager using DataUpdateCoordinator."""

from __future__ import annotations

import hashlib
import json
import logging
import traceback
from base64 import b64decode
from datetime import datetime, timedelta
from typing import Any

from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.providers import homeassistant as auth_ha
from homeassistant.components import bluetooth, mqtt
from homeassistant.components.number import NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from paho.mqtt import client as mqtt_client
from paho.mqtt import enums as mqtt_enums

from custom_components.zendure_ha.devices.solarflow800Pro import SolarFlow800Pro

from .api import Api
from .const import CONF_MQTTLOCAL, CONF_MQTTLOG, CONF_P1METER, CONF_WIFIPSW, CONF_WIFISSID, DOMAIN, ManagerState, SmartMode
from .devices.ace1500 import ACE1500
from .devices.aio2400 import AIO2400
from .devices.hub1200 import Hub1200
from .devices.hub2000 import Hub2000
from .devices.hyper2000 import Hyper2000
from .devices.solarflow800 import SolarFlow800
from .devices.solarflow2400ac import SolarFlow2400AC
from .number import ZendureNumber
from .select import ZendureSelect
from .zendurebase import ZendureBase
from .zenduredevice import ZendureDevice

_LOGGER = logging.getLogger(__name__)


class ZendureManager(DataUpdateCoordinator[int], ZendureBase):
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
        ZendureBase.__init__(self, hass, "Zendure Manager", "Zendure Manager", "1.0.41")

        self.p1meter = config_entry.data.get(CONF_P1METER)
        self.operation = 0
        self.setpoint = 0
        self.zero_idle = datetime.max
        self.zero_next = datetime.min
        self.zero_fast = datetime.min

        # initialize mqtt
        ZendureDevice.mqttIsLocal = config_entry.data.get(CONF_MQTTLOCAL, False)
        ZendureDevice.mqttLog = config_entry.data.get(CONF_MQTTLOG, False)
        ZendureDevice.wifissid = config_entry.data.get(CONF_WIFISSID, None)
        ZendureDevice.wifipsw = config_entry.data.get(CONF_WIFIPSW, None)

        # Create the api
        self.api = Api(hass, dict(config_entry.data))

    async def load(self) -> bool:
        """Initialize the manager."""
        try:
            if not await self.api.connect():
                _LOGGER.error("Unable to connect to Zendure API")
                return False

            # create and initialize the devices
            await self.createDevices()
            _LOGGER.info(f"Found: {len(ZendureDevice.devicedict)} devices")

            # Add ZendureManager sensors
            _LOGGER.info(f"Adding sensors {self.name}")
            selects = [
                self.select("Operation", {0: "off", 1: "manual", 2: "smart"}, self.update_operation, True),
            ]
            ZendureSelect.add(selects)

            numbers = [
                self.number("manual_power", None, "W", "power", -10000, 10000, NumberMode.BOX, self._update_manual_energy),
            ]
            ZendureNumber.add(numbers)

            # Set sensors from values entered in config flow setup
            if self.p1meter:
                _LOGGER.info(f"Energy sensors: {self.p1meter} to _update_smart_energyp1")
                async_track_state_change_event(self.hass, [self.p1meter], self._update_smart_energyp1)

            # create the mqtt client
            info = self.hass.config_entries.async_loaded_entries(mqtt.DOMAIN)
            if info is None or len(info) == 0 or self.hass.config.api.local_ip is None:
                _LOGGER.info("No MQTT integration found")
                ZendureDevice.mqttIsLocal = False

            ZendureDevice.mqttCloudUrl = self.api.mqttUrl
            if ZendureDevice.mqttIsLocal:
                ZendureDevice.mqttLocalUrl = self.hass.config.api.local_ip
                ZendureDevice.mqttClient.__init__(mqtt_enums.CallbackAPIVersion.VERSION1, "zendureMqtt", False, 1)
                ZendureDevice.mqttClient.username_pw_set("zendureMqtt", await self.mqttUser("zendureMqtt"))
                ZendureDevice.mqttClient.connect(ZendureDevice.mqttLocalUrl, 1883)

            else:
                ZendureDevice.mqttClient.__init__(mqtt_enums.CallbackAPIVersion.VERSION1, self.api.token, False, 0)
                ZendureDevice.mqttClient.username_pw_set("zenApp", b64decode(self.api.mqttinfo.encode()).decode("latin-1"))
                ZendureDevice.mqttClient.connect(self.api.mqttUrl, 1883)

            ZendureDevice.mqttClient.on_connect = self.mqttConnect
            ZendureDevice.mqttClient.on_message = self.mqttMessage
            ZendureDevice.mqttClient.suppress_exceptions = True
            ZendureDevice.mqttClient.loop_start()

            for device in ZendureDevice.devices:
                await device.mqttServer()
                device.mqttRefresh()

            _LOGGER.info("Zendure Manager initialized")

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())
            return False
        return True

    async def unload(self) -> None:
        """Unload the manager."""
        if ZendureDevice.mqttClient.is_connected():
            for device in ZendureDevice.devices:
                ZendureDevice.mqttClient.unsubscribe(f"/{device.prodkey}/{device.deviceId}/#")
                ZendureDevice.mqttClient.unsubscribe(f"iot/{device.prodkey}/{device.deviceId}/#")

                if device.deviceMqtt is not None and device.deviceMqtt.is_connected():
                    device.deviceMqtt.unsubscribe(f"/{device.prodkey}/{device.deviceId}/#")
                    device.deviceMqtt.unsubscribe(f"iot/{device.prodkey}/{device.deviceId}/#")
                    device.deviceMqtt.loop_stop()
                    device.deviceMqtt.disconnect()

            ZendureDevice.mqttClient.loop_stop()
            ZendureDevice.mqttClient.disconnect()

        ZendureDevice.devicedict.clear()
        ZendureDevice.devices.clear()
        ZendureDevice.clusters.clear()

    async def createDevices(self) -> None:
        # Create the devices
        deviceInfo = await self.api.getDevices()
        for dev in deviceInfo:
            if (deviceId := dev["deviceKey"]) is None or (prodName := dev["productName"]) is None:
                continue
            _LOGGER.info(f"Adding device: {deviceId} {prodName}")
            _LOGGER.info(f"Data: {dev}")

            try:
                match prodName.lower():
                    case "hyper 2000":
                        device = Hyper2000(self.hass, deviceId, prodName, dev)
                    case "solarflow 800":
                        device = SolarFlow800(self.hass, deviceId, prodName, dev)
                    case "solarflow2.0":
                        device = Hub1200(self.hass, deviceId, prodName, dev)
                    case "solarflow hub 2000":
                        device = Hub2000(self.hass, deviceId, prodName, dev)
                        if (packList := dev.get("packList", None)) is not None:
                            for pack in packList:
                                if pack.get("productName", None) == "Ace 1500":
                                    _LOGGER.info(f"{device.name} Adding Ace 1500 from packList")
                                    aceId = pack["deviceKey"]
                                    ZendureDevice.devicedict[aceId] = ACE1500(self.hass, aceId, pack["productName"], pack, device.name)

                    case "solarflow aio zy":
                        device = AIO2400(self.hass, deviceId, prodName, dev)
                    case "ace 1500":
                        device = ACE1500(self.hass, deviceId, prodName, dev)
                    case "solarflow 800 pro":
                        device = SolarFlow800Pro(self.hass, deviceId, prodName, dev)
                    case "solarflow 2400 ac":
                        device = SolarFlow2400AC(self.hass, deviceId, prodName, dev)
                    case _:
                        _LOGGER.info(f"Device {prodName} is not supported!")
                        continue
                ZendureDevice.devicedict[deviceId] = device

                # if ZendureDevice.mqttIsLocal:
                device.deviceMqttClient(await self.mqttUser(device.deviceId))

            except Exception as err:
                _LOGGER.error(err)
                _LOGGER.error(traceback.format_exc())

        # create the sensors
        for device in ZendureDevice.devicedict.values():
            device.entitiesCreate()

    async def _async_update_data(self) -> int:
        """Refresh the data of all devices's."""
        _LOGGER.info("refresh devices")
        try:
            reset = datetime.now() - timedelta(seconds=300)
            midnight = datetime.now().date() != reset.date()

            def isBleDevice(device: ZendureDevice, si: bluetooth.BluetoothServiceInfoBleak) -> bool:
                if si.name.startswith("Zen") and (bts := si.manufacturer_data.get(17733, None)) is not None:
                    _LOGGER.info(f"Found Zendure Bluetooth device: {si}")
                    return device.snNumber.endswith(bts.decode("utf8")[:-1])
                return False

            for device in ZendureDevice.devices:
                if device.service_info is None:
                    device.service_info = next((si for si in bluetooth.async_discovered_service_info(self.hass, False) if isBleDevice(device, si)), None)

                if device.online_mqtt < reset:
                    device.online_mqtt = datetime.min
                    device.setvalue("MqttOnline", False)
                    await device.mqttServer()
                    continue

                # reset connection at midnight
                if midnight:
                    await device.mqttServer()
                device.mqttRefresh()

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())

        if self.hass and self.hass.loop.is_running():
            self._schedule_refresh()
        return 0

    def update_operation(self, _entity: ZendureSelect, operation: int) -> None:
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

    async def mqttUser(self, username: str) -> str:
        """Ensure the user exists."""
        psw = hashlib.md5(username.encode()).hexdigest().upper()[8:24]  # noqa: S324
        try:
            provider: auth_ha.HassAuthProvider = auth_ha.async_get_provider(self.hass)
            credentials = await provider.async_get_or_create_credentials({"username": username.lower()})
            user = await self.hass.auth.async_get_user_by_credentials(credentials)
            if user is None:
                user = await self.hass.auth.async_create_user(username, group_ids=[GROUP_ID_USER], local_only=False)
                await provider.async_add_auth(username.lower(), psw)
                await self.hass.auth.async_link_user(user, credentials)

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())
        return psw

    def mqttConnect(self, client: Any, _userdata: Any, _flags: Any, rc: Any) -> None:
        if rc == 0:
            for device in ZendureDevice.devices:
                client.subscribe(f"/{device.prodkey}/{device.deviceId}/#")
                client.subscribe(f"iot/{device.prodkey}/{device.deviceId}/#")
                device.mqttRefresh()
        else:
            _LOGGER.error(f"Unable to connect to MQTT broker, return code: {rc}")

    def mqttDisconnect(self, _client: Any, _userdata: Any, rc: Any, _props: Any) -> None:
        _LOGGER.info(f"Client disconnected from MQTT broker with return code {rc}")

    def mqttMessage(self, _client: Any, _userdata: Any, msg: Any) -> None:
        try:
            # check for valid device in payload
            topics = msg.topic.split("/")
            payload = json.loads(msg.payload.decode())
            payload.pop("deviceId", None)
            deviceId = topics[2]
            if (device := ZendureDevice.devicedict.get(deviceId, None)) is not None:
                topics[2] = device.name
                if ZendureDevice.mqttLog:
                    _LOGGER.info(f"Topic: {self.name} {msg.topic.replace(deviceId, device.name)} => {payload}")
                device.mqttMessage(topics, payload)

                # check if we must relay to the cloud
                if not ZendureDevice.mqttIsLocal or device.online_zenApp < datetime.now():
                    if topics[-1] in ["read", "write", "replay", "connected", "invoke"]:
                        return

                    if topics[-1] in ["reply", "log", "report", "error", "config", "device"]:
                        device.deviceMqtt.publish(msg.topic, msg.payload)
                        return
                    _LOGGER.info(f"Zendure local => {self.name} => {msg.topic} {msg.payload}")
            else:
                _LOGGER.info(f"Unknown device: {deviceId} => {msg.topic} => {payload}")

        except:  # noqa: E722
            return

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
                d.powerAct = d.asInt("outputHomePower") - (d.asInt("outputPackPower") - d.asInt("solarInputPower"))
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
                if p1 < -SmartMode.MIN_POWER:
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
                d.capacity = max(0, d.kwh * (d.asInt("electricLevel") - d.asInt("minSoc")))
                totalPower += d.powerMax
            else:
                d.capacity = max(0, d.kwh * (d.asInt("socSet") - d.asInt("electricLevel")))
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
                    pwr += d.offset
                power -= pwr

                # update the device
                d.writePower(pwr, True)
