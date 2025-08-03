"""Coordinator for Zendure integration."""

from __future__ import annotations

import hashlib
import logging
import traceback
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from math import sqrt
from typing import Any

from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.providers import homeassistant as auth_ha
from homeassistant.components import bluetooth
from homeassistant.components.number import NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import Api
from .cluster import Cluster
from .const import CONF_P1METER, DOMAIN, ManagerState, SmartMode
from .device import ZendureDevice
from .entity import EntityDevice
from .number import ZendureRestoreNumber
from .select import ZendureRestoreSelect, ZendureSelect

SCAN_INTERVAL = timedelta(seconds=90)

_LOGGER = logging.getLogger(__name__)

type ZendureConfigEntry = ConfigEntry[ZendureManager]


class ZendureManager(DataUpdateCoordinator[None], EntityDevice):
    """Class to regular update devices."""

    devices: list[ZendureDevice] = []
    clusters: dict[str, Cluster] = {}

    def __init__(self, hass: HomeAssistant, entry: ZendureConfigEntry) -> None:
        """Initialize Zendure Manager."""
        super().__init__(hass, _LOGGER, name="Zendure Manager", update_interval=SCAN_INTERVAL, config_entry=entry)
        EntityDevice.__init__(self, hass, "manager", "Zendure Manager", "Zendure Manager")
        self.operation = 0
        self.setpoint = 0
        self.zero_idle = datetime.max
        self.zero_next = datetime.min
        self.zero_fast = datetime.min
        self.check_reset = datetime.min
        self.zorder: deque[int] = deque([25, -25], maxlen=8)
        self.cluster: dict[str, Cluster] = {}
        self.p1meterEvent: Callable[[], None] | None = None
        self.update_p1meter(entry.data.get(CONF_P1METER, "sensor.power_actual"))
        self.operationmode = (
            ZendureRestoreSelect(self, "Operation", {0: "off", 1: "manual", 2: "smart", 3: "smart_discharging", 4: "smart_charging"}, self.update_operation),
        )
        self.manualpower = ZendureRestoreNumber(self, "manual_power", self._update_manual_energy, None, "W", "power", 10000, -10000, NumberMode.BOX)
        self.api = Api()
        self.update_count = 0

    async def loadDevices(self) -> None:
        if self.config_entry is None or (data := await Api.Connect(self.hass, dict(self.config_entry.data))) is None:
            return

        # updateCluster callback
        def updateCluster(_entity: ZendureRestoreSelect, _value: Any) -> None:
            self.update_clusters()

        # load devices
        device_registry = dr.async_get(self.hass)
        for dev in data["deviceList"]:
            try:
                if (deviceId := dev["deviceKey"]) is None or (prodModel := dev["productModel"]) is None:
                    continue
                _LOGGER.info(f"Adding device: {deviceId} {prodModel} => {dev}")

                init = Api.createdevice.get(prodModel.lower(), None)
                if init is None:
                    _LOGGER.info(f"Device {prodModel} is not supported!")
                    continue

                # create the device and mqtt server
                device = init(self.hass, deviceId, prodModel, dev)
                if di := device_registry.async_get_device(identifiers={(DOMAIN, device.name)}):
                    device.attr_device_info["connections"] = di.connections

                self.devices.append(device)
                Api.devices[deviceId] = device
                device.cluster.onchanged = updateCluster

                try:
                    psw = hashlib.md5(deviceId.encode()).hexdigest().upper()[8:24]  # noqa: S324
                    provider: auth_ha.HassAuthProvider = auth_ha.async_get_provider(self.hass)
                    credentials = await provider.async_get_or_create_credentials({"username": deviceId.lower()})
                    user = await self.hass.auth.async_get_user_by_credentials(credentials)
                    if user is None:
                        user = await self.hass.auth.async_create_user(deviceId, group_ids=[GROUP_ID_USER], local_only=False)
                        await provider.async_add_auth(deviceId.lower(), psw)
                        await self.hass.auth.async_link_user(user, credentials)
                    else:
                        await provider.async_change_password(deviceId.lower(), psw)

                    _LOGGER.info(f"Created MQTT user: {deviceId} with password: {psw}")

                except Exception as err:
                    _LOGGER.error(err)

            except Exception as e:
                _LOGGER.error(f"Unable to create device {e}!")
                _LOGGER.error(traceback.format_exc())

        _LOGGER.info(f"Loaded {len(self.devices)} devices")
        self.update_clusters()

        # initialize the api
        self.api.Init(self.config_entry.data, data["mqtt"])

    async def _async_update_data(self) -> None:
        _LOGGER.debug("Updating Zendure data")

        def isBleDevice(device: ZendureDevice, si: bluetooth.BluetoothServiceInfoBleak) -> bool:
            for d in si.manufacturer_data.values():
                try:
                    if d is None or len(d) <= 1:
                        continue
                    sn = d.decode("utf8")[:-1]
                    if device.snNumber.endswith(sn):
                        _LOGGER.info(f"Found Zendure Bluetooth device: {si}")
                        device.attr_device_info["connections"] = {("bluetooth", str(si.address))}
                        return True
                except Exception:  # noqa: S112
                    continue
            return False

        for device in self.devices:
            if self.attr_device_info.get("connections", None) is None:
                for si in bluetooth.async_discovered_service_info(self.hass, False):
                    if isBleDevice(device, si):
                        break

            _LOGGER.debug(f"Update device: {device.name} ({device.deviceId})")
            await device.dataRefresh(self.update_count)
        self.update_count += 1

        # Manually update the timer
        if self.hass and self.hass.loop.is_running():
            self._schedule_refresh()

    def update_p1meter(self, p1meter: str | None) -> None:
        """Update the P1 meter sensor."""
        _LOGGER.debug("Updating P1 meter to: %s", p1meter)
        if self.p1meterEvent:
            self.p1meterEvent()
        if p1meter:
            self.p1meterEvent = async_track_state_change_event(self.hass, [p1meter], self._p1_changed)
        else:
            self.p1meterEvent = None

    @callback
    async def _p1_changed(self, event: Event[EventStateChangedData]) -> None:
        try:
            # exit if there is nothing to do
            if not self.hass.is_running or not self.hass.is_running or (new_state := event.data["new_state"]) is None:
                return

            try:  # convert the state to a float
                p1 = int(float(new_state.state))
            except ValueError:
                return

            # calculate the standard deviation
            avg = sum(self.zorder) / len(self.zorder) if len(self.zorder) > 1 else 0
            stddev = min(50, sqrt(sum([pow(i - avg, 2) for i in self.zorder]) / len(self.zorder)))
            if isFast := abs(p1 - avg) > SmartMode.Threshold * stddev:
                self.zorder.clear()
            self.zorder.append(p1)

            # check minimal time between updates
            time = datetime.now()
            if time < self.zero_next or (time < self.zero_fast and not isFast):
                return

            # get the current power
            powerActual = 0
            for d in self.devices:
                d.powerAct = await d.power_get()
                powerActual += d.powerAct

            _LOGGER.info(f"Update p1: {p1} power: {powerActual} operation: {self.operation} delta:{p1 - avg} stddev: {stddev} fast: {isFast}")
            match self.operation:
                case SmartMode.NONE:
                    return
                case SmartMode.MATCHING:
                    if powerActual < 0:  # update when we are charging
                        self.update_power(min(0, powerActual + p1), ManagerState.CHARGING)
                    elif powerActual > 0:  # update when we are discharging
                        self.update_power(max(0, powerActual + p1), ManagerState.DISCHARGING)
                    elif self.zero_idle == datetime.max:  # check if it is the first time we are idle
                        _LOGGER.info(f"Wait {SmartMode.TIMEIDLE} sec for state change p1: {p1}")
                        self.zero_idle = time + timedelta(seconds=SmartMode.TIMEIDLE)
                    elif self.zero_idle < time:  # update when we are idle for more than SmartMode.TIMEIDLE seconds
                        if p1 < -SmartMode.MIN_POWER:
                            _LOGGER.info(f"Start charging with p1: {p1}")
                            self.update_power(p1, ManagerState.CHARGING)
                            self.zero_idle = datetime.max
                        elif p1 >= 0:
                            _LOGGER.info(f"Start discharging with p1: {p1}")
                            self.update_power(p1, ManagerState.DISCHARGING)
                            self.zero_idle = datetime.max
                        else:
                            _LOGGER.info(f"Unable to charge/discharge p1: {p1}")

                case SmartMode.MATCHING_DISCHARGE:
                    self.update_power(max(0, powerActual + p1), ManagerState.DISCHARGING)

                case SmartMode.MATCHING_CHARGE:
                    pwr = powerActual + p1 if powerActual < 0 else p1 if p1 < -SmartMode.MIN_POWER else 0
                    self.update_power(min(0, pwr), ManagerState.CHARGING)

                case SmartMode.MANUAL:
                    self.update_power(self.setpoint, ManagerState.DISCHARGING if self.setpoint >= 0 else ManagerState.CHARGING)

            self.zero_next = time + timedelta(seconds=SmartMode.TIMEZERO)
            self.zero_fast = time + timedelta(seconds=SmartMode.TIMEFAST)

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())

    def update_power(self, power: int, state: ManagerState) -> None:
        # get the total capacity of all clusters
        availPower = sum(c.initCluster(state) for c in self.cluster.values())
        _LOGGER.info(f"Update setpoint: {power} state{state} max power: {availPower}")

        # distribute the power over clusters
        clusters = sorted(self.cluster.values(), key=lambda d: d.capacity, reverse=False)
        for c in clusters:
            clusterPower = c.clusterPower(power, availPower)
            clusterDevices = sorted(c.devices, key=lambda d: d.capacity, reverse=False)

            # set the device power
            for d in clusterDevices:
                pwr = c.devicePower(clusterPower, c.powerTotal, d)
                _LOGGER.debug(f"Set power for device: {d.name} ({d.capacity}) to {pwr}W")
                pwr = d.power_set(state, pwr)
                availPower -= d.powerAvail
                c.powerTotal -= d.powerAvail
                clusterPower -= pwr
                power -= pwr

    def update_clusters(self) -> None:
        _LOGGER.info("Update clusters")

        self.cluster.clear()
        for device in self.devices:
            try:
                match device.cluster.state:
                    case "clusterowncircuit" | "cluster3600":
                        cluster = Cluster(device, [], 3600, -3600)
                    case "cluster800":
                        cluster = Cluster(device, [], 800, -1200)
                    case "cluster1200":
                        cluster = Cluster(device, [], 1200, -1800)
                    case "cluster2400":
                        cluster = Cluster(device, [], 2400, -3600)
                    case _:
                        continue
                cluster.devices.append(device)
                self.cluster[device.deviceId] = cluster
            except:  # noqa: E722
                _LOGGER.error(f"Unable to create cluster for device: {device.name} ({device.deviceId})")

        # Update the clusters and select optins for each device
        for device in self.devices:
            try:
                clusters: dict[Any, str] = {0: "unused", 1: "clusterowncircuit", 2: "cluster800", 3: "cluster1200", 4: "cluster2400", 5: "cluster3600"}
                for c in self.cluster.values():
                    if c.device.deviceId != device.deviceId:
                        clusters[c.device.deviceId] = f"Part of {c.device.name} cluster"
                device.cluster.setDict(clusters)
            except:  # noqa: E722
                _LOGGER.error(f"Unable to create cluster for device: {device.name} ({device.deviceId})")

        # Add devices to clusters
        for device in self.devices:
            if clstr := self.cluster.get(device.cluster.value):
                clstr.devices.append(device)

    async def update_operation(self, entity: ZendureSelect, _operation: Any) -> None:
        operation = int(entity.value)
        _LOGGER.info(f"Update operation: {operation} from: {self.operation}")
        self.operation = operation
        if self.operation != SmartMode.MATCHING and len(self.devices) > 0:
            for d in self.devices:
                d.power_set(ManagerState.IDLE, 0)
        if self.operation != SmartMode.NONE and len(self.devices) > 0:
            for d in self.devices:
                await d.entityWrite(d.limitOutput, 0)
                await d.entityWrite(d.limitInput, 0)

    def _update_manual_energy(self, _number: Any, power: float) -> None:
        try:
            if self.operation == SmartMode.MANUAL:
                self.setpoint = int(power)
                self.update_power(self.setpoint, ManagerState.DISCHARGING if power >= 0 else ManagerState.CHARGING)

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())
