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
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.loader import async_get_integration

from .api import Api
from .const import CONF_P1METER, DOMAIN, ManagerState, SmartMode
from .device import ZendureDevice, ZendureLegacy
from .entity import EntityDevice
from .fusegroup import FuseGroup
from .number import ZendureRestoreNumber
from .select import ZendureRestoreSelect, ZendureSelect
from .sensor import ZendureSensor

SCAN_INTERVAL = timedelta(seconds=90)

_LOGGER = logging.getLogger(__name__)

type ZendureConfigEntry = ConfigEntry[ZendureManager]


class ZendureManager(DataUpdateCoordinator[None], EntityDevice):
    """Class to regular update devices."""

    devices: list[ZendureDevice] = []
    fuseGroups: dict[str, FuseGroup] = {}

    def __init__(self, hass: HomeAssistant, entry: ZendureConfigEntry) -> None:
        """Initialize Zendure Manager."""
        super().__init__(hass, _LOGGER, name="Zendure Manager", update_interval=SCAN_INTERVAL, config_entry=entry)
        EntityDevice.__init__(self, hass, "manager", "Zendure Manager", "Zendure Manager")
        self.operation = 0
        self.setpoint: int = 0
        self.last_delta: int = SmartMode.TIMEIDLE
        self.last_discharge = datetime.max
        self.mode_idle = datetime.max
        self.zero_next = datetime.min
        self.zero_fast = datetime.min
        self.check_reset = datetime.min
        self.zorder: deque[int] = deque([25, -25], maxlen=8)
        self.fuseGroup: dict[str, FuseGroup] = {}
        self.p1meterEvent: Callable[[], None] | None = None
        self.api = Api()
        self.update_count = 0

    async def loadDevices(self) -> None:
        if self.config_entry is None or (data := await Api.Connect(self.hass, dict(self.config_entry.data), True)) is None:
            return
        if (mqtt := data.get("mqtt")) is None:
            return

        # get version number from integration
        integration = await async_get_integration(self.hass, DOMAIN)
        if integration is None:
            _LOGGER.error("Integration not found for domain: %s", DOMAIN)
            return
        self.attr_device_info["sw_version"] = integration.manifest.get("version", "unknown")

        self.operationmode = (
            ZendureRestoreSelect(self, "Operation", {0: "off", 1: "manual", 2: "smart", 3: "smart_discharging", 4: "smart_charging"}, self.update_operation),
        )
        self.manualpower = ZendureRestoreNumber(self, "manual_power", self._update_manual_energy, None, "W", "power", 10000, -10000, NumberMode.BOX)
        self.availableKwh = ZendureSensor(self, "available_kwh", None, "kWh", "energy", None, 1)
        self.power = ZendureSensor(self, "power", None, "W", "power", None, 0)

        # load devices
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
                self.devices.append(device)
                Api.devices[deviceId] = device

                if Api.localServer is not None and Api.localServer != "":
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

        # initialize the api & p1 meter
        await EntityDevice.add_entities()
        self.api.Init(self.config_entry.data, mqtt)
        self.update_p1meter(self.config_entry.data.get(CONF_P1METER, "sensor.power_actual"))
        self.update_fusegroups()

    async def _async_update_data(self) -> None:
        _LOGGER.debug("Updating Zendure data")
        await EntityDevice.add_entities()

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
            if isinstance(device, ZendureLegacy) and device.bleMac is None:
                for si in bluetooth.async_discovered_service_info(self.hass, False):
                    if isBleDevice(device, si):
                        break

            _LOGGER.debug(f"Update device: {device.name} ({device.deviceId})")
            device.setStatus()
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
            # update new entities
            await EntityDevice.add_entities()

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
            powerAct = 0
            for d in self.devices:
                d.powerAct = await d.power_get()
                powerActual += d.powerAct
                powerAct += d.powerAct
            self.power.update_value(powerActual)

            _LOGGER.info(f"Update p1: {p1} power: {powerActual} operation: {self.operation} delta:{p1 - avg} stddev: {stddev} fast: {isFast}")
            match self.operation:
                case SmartMode.MATCHING:
                    # update when we are discharging
                    if powerActual > 0:
                        self.update_power(max(0, powerActual + p1), ManagerState.DISCHARGING)
                        self.mode_idle = datetime.max
                        self.last_discharge = time

                    # update when we are charging
                    elif powerActual < 0:
                        self.update_power(min(0, powerActual + p1), ManagerState.CHARGING)
                        self.mode_idle = datetime.max

                    # start discharging immediately
                    elif p1 >= 0:
                        self.update_power(p1, ManagerState.DISCHARGING)
                        delta = int((time - self.last_discharge).total_seconds())
                        self.last_delta = SmartMode.TIMEIDLE + (0 if delta < SmartMode.TIMEIDLE or self.last_delta > SmartMode.TIMERESET else delta)
                        _LOGGER.info(f"Update last_delta: {self.last_delta}")

                    # determine the idle delay
                    elif self.mode_idle == datetime.max:
                        self.mode_idle = time + timedelta(seconds=self.last_delta)

                    # idle long enough and power enough => start charging
                    elif self.mode_idle < time and abs(p1) > SmartMode.MIN_POWER:
                        self.update_power(p1, ManagerState.CHARGING)

                case SmartMode.MATCHING_DISCHARGE:
                    self.update_power(max(0, powerActual + p1), ManagerState.DISCHARGING)

                case SmartMode.MATCHING_CHARGE:
                    # in update_power(), the upper limit for pwr is solarInputPower, if there is a need for power into the house grid (pwr > 0)
                    # this is not a real "discharge", because no power go out of the battery, however, the device deliver power to the house
                    # if pwr is negative (AC load), all the power goes into battery
                    pwr = powerActual + p1
                    self.update_power(pwr, ManagerState.CHARGING if pwr < 0 else ManagerState.DISCHARGING)

                case SmartMode.MANUAL:
                    self.update_power(self.setpoint, ManagerState.DISCHARGING if self.setpoint >= 0 else ManagerState.CHARGING)

            self.zero_next = time + timedelta(seconds=SmartMode.TIMEZERO)
            self.zero_fast = time + timedelta(seconds=SmartMode.TIMEFAST)

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())

    def update_power(self, power: int, state: ManagerState) -> None:
        """Update the power for all devices."""
        if len(self.devices) == 0:
            return

        isCharging = state == ManagerState.CHARGING

        # int the fusegroups
        for g in self.fuseGroup.values():
            g.powerAvail = g.minpower if isCharging else g.maxpower
            g.powerUsed = 0

        maxPower = 0
        totalKwh = 0
        devs = list[ZendureDevice]()
        for d in sorted(self.devices, key=lambda d: int(d.availableKwh.asNumber * 2), reverse=not isCharging):
            if (g := d.fusegroup) is not None and d.online:
                useDevice = abs(0.85 * maxPower) < abs(power) if d.powerAct == 0 else abs(0.80 * maxPower) < abs(power)
                totalKwh += d.availableKwh.asNumber
                if g.powerAvail == g.powerUsed or not useDevice or d.power_limit(state):
                    d.power_set(state, 0)
                else:
                    d.powerAvail = max(g.powerAvail - g.powerUsed, d.powerMin) if isCharging else min(g.powerAvail - g.powerUsed, d.powerMax)
                    g.powerUsed += d.powerAvail
                    maxPower += d.powerAvail
                    devs.append(d)
        self.availableKwh.update_value(totalKwh)

        if len(devs) > 0:
            for g in self.fuseGroup.values():
                g.powerUsed = 0

            while len(devs) > 0:
                d = devs.pop(0)
                g = d.fusegroup
                if g is None:
                    continue
                pwr = power * d.powerAvail / (maxPower if maxPower != 0 else 1)
                # limit power to solar input if smart charge only is selected

                # adjust the power for the fusegroup
                pwr = int(max(g.powerAvail - g.powerUsed, pwr) if isCharging else min(g.powerAvail - g.powerUsed, pwr))

                maxPower -= d.powerAvail
                pwr = max(d.powerMin, pwr) if isCharging else min(d.powerMax, pwr)
                # limit on smart charge the power to the solarInputPower, no power goes in or out of the battery
                # if pwr is positive (no AC load) and solarInputPower < pwr
                # in case of AC load, pwr is negative and the solarInputPower + pwr goes into battery
                if self.operation == SmartMode.MATCHING_CHARGE and d.solarInputPower.asNumber is not None:
                    pwr = min(pwr, d.solarInputPower.asNumber)
                pwr = d.power_set(state, pwr)

                # update the totals
                power -= pwr
                g.powerUsed += pwr

    def update_fusegroups(self) -> None:
        _LOGGER.info("Update fusegroups")

        # updateFuseGroup callback
        def updateFuseGroup(_entity: ZendureRestoreSelect, _value: Any) -> None:
            self.update_fusegroups()

        self.fuseGroup.clear()
        for device in self.devices:
            try:
                if device.fuseGroup.onchanged is None:
                    device.fuseGroup.onchanged = updateFuseGroup

                match device.fuseGroup.state:
                    case "owncircuit" | "group3600":
                        fusegroup = FuseGroup(device.name, device.deviceId, 3600, -3600)
                    case "group800":
                        fusegroup = FuseGroup(device.name, device.deviceId, 800, -1200)
                    case "group1200":
                        fusegroup = FuseGroup(device.name, device.deviceId, 1200, -1200)
                    case "group2000":
                        fusegroup = FuseGroup(device.name, device.deviceId, 2000, -2000)
                    case "group2400":
                        fusegroup = FuseGroup(device.name, device.deviceId, 2400, -2400)
                    case _:
                        continue

                device.fusegroup = fusegroup
                self.fuseGroup[device.deviceId] = fusegroup
            except:  # noqa: E722
                _LOGGER.error(f"Unable to create fusegroup for device: {device.name} ({device.deviceId})")

        # Update the fusegroups and select optins for each device
        for device in self.devices:
            try:
                fusegroups: dict[Any, str] = {
                    0: "unused",
                    1: "owncircuit",
                    2: "group800",
                    3: "group1200",
                    4: "group2000",
                    5: "group2400",
                    6: "group3600",
                }
                for c in self.fuseGroup.values():
                    if c.deviceId != device.deviceId:
                        fusegroups[c.deviceId] = f"Part of {c.name} fusegroup"
                device.fuseGroup.setDict(fusegroups)
            except:  # noqa: E722
                _LOGGER.error(f"Unable to create fusegroup for device: {device.name} ({device.deviceId})")

        # Add devices to fusegroups
        for device in self.devices:
            if grp := self.fuseGroup.get(device.fuseGroup.value):
                device.fusegroup = grp
            device.setStatus()

    def update_operation(self, entity: ZendureSelect, _operation: Any) -> None:
        operation = int(entity.value)
        _LOGGER.info(f"Update operation: {operation} from: {self.operation}")
        self.operation = operation

        match self.operation:
            case SmartMode.NONE:
                if len(self.devices) > 0:
                    for d in self.devices:
                        d.power_set(ManagerState.IDLE, 0)

            case SmartMode.MANUAL:
                self.update_power(self.setpoint, ManagerState.DISCHARGING if self.setpoint >= 0 else ManagerState.CHARGING)

    def _update_manual_energy(self, _number: Any, power: float) -> None:
        try:
            if self.operation == SmartMode.MANUAL:
                self.setpoint = int(power)
                self.update_power(self.setpoint, ManagerState.DISCHARGING if power >= 0 else ManagerState.CHARGING)

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())
