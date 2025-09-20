"""Coordinator for Zendure integration."""

from __future__ import annotations

import asyncio
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
from homeassistant.components import bluetooth, persistent_notification
from homeassistant.components.number import NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.loader import async_get_integration

from .api import Api
from .const import CONF_P1METER, DOMAIN, DeviceState, SmartMode
from .device import ZendureDevice, ZendureLegacy
from .entity import EntityDevice
from .fusegroup import FuseGroup
from .number import ZendureRestoreNumber
from .select import ZendureRestoreSelect, ZendureSelect
from .sensor import ZendureSensor
from .binary_sensor import ZendureBinarySensor

SCAN_INTERVAL = timedelta(seconds=90)

_LOGGER = logging.getLogger(__name__)

type ZendureConfigEntry = ConfigEntry[ZendureManager]


class ZendureManager(DataUpdateCoordinator[None], EntityDevice):
    """Class to regular update devices."""

    devices: list[ZendureDevice] = []
    fuseGroups: list[FuseGroup] = []

    def __init__(self, hass: HomeAssistant, entry: ZendureConfigEntry) -> None:
        """Initialize Zendure Manager."""
        super().__init__(hass, _LOGGER, name="Zendure Manager", update_interval=SCAN_INTERVAL, config_entry=entry)
        EntityDevice.__init__(self, hass, "manager", "Zendure Manager", "Zendure Manager")
        self.operation = 0
        self.zero_next = datetime.min
        self.zero_fast = datetime.min
        self.check_reset = datetime.min
        self.p1_history: deque[int] = deque([25, -25], maxlen=8)
        self.power_history: deque[int] = deque(maxlen=5)
        self.p1_stddev_history: deque[int] = deque(maxlen=25)
        self.p1meterEvent: Callable[[], None] | None = None
        self.api = Api()
        self.update_count = 0
        self.total = 0

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
        self.manualpower = ZendureRestoreNumber(self, "manual_power", None, None, "W", "power", 10000, -10000, NumberMode.BOX, True)
        self.availableKwh = ZendureSensor(self, "available_kwh", None, "kWh", "energy", None, 1)
        self.power = ZendureSensor(self, "power", None, "W", "power", None, 0)
        self.p1_avg = ZendureSensor(self, "p1_avg", None, "W", "power", None, 0)
        self.p1_stddev = ZendureSensor(self, "p1_stddev", None, "W", "power", None, 0)
        self.p1_powerAverage = ZendureSensor(self, "p1_powerAverage", None, "W", "power", None, 0)
        self.solardischarge = ZendureBinarySensor(self, "solardischarge")
        self.batterydischarge = ZendureBinarySensor(self, "batterydischarge")
        self.batterycharge = ZendureBinarySensor(self, "batterycharge")
        self.isfast = ZendureBinarySensor(self, "isfast")

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
        await asyncio.sleep(1)  # allow other tasks to run
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
            await device.dataRefresh(self.update_count)
            device.setStatus()
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
        # update new entities
        await EntityDevice.add_entities()

        # exit if there is nothing to do
        if not self.hass.is_running or not self.hass.is_running or (new_state := event.data["new_state"]) is None:
            return

        try:  # convert the state to a float
            p1 = int(float(new_state.state))
        except ValueError:
            return

        # Check for fast delay
        time = datetime.now()
        if time < self.zero_fast:
            self.p1_history.append(p1)
            return

        # calculate the standard deviation
        if len(self.p1_history) > 1:
            avg = int(sum(self.p1_history) / len(self.p1_history))
            self.p1_avg.update_value(avg)
            stddev = min(50, sqrt(sum([pow(i - avg, 2) for i in self.p1_history]) / len(self.p1_history)))

            self.p1_stddev_history.append(stddev)

            self.p1_stddev.update_value(stddev)
            self.isfast.update_value(False)
            if isFast := abs(p1 - avg) > SmartMode.Threshold * stddev:
                self.p1_history.clear()
                self.isfast.update_value(True)
        else:
            isFast = False
        self.p1_history.append(p1)

        # check minimal time between updates
        if isFast or time > self.zero_next:
            try:
                self.zero_next = time + timedelta(seconds=SmartMode.TIMEZERO)
                if isFast:
                    self.zero_fast = self.zero_next
                    await self.powerChanged(p1, True)
                else:
                    self.zero_fast = time + timedelta(seconds=SmartMode.TIMEFAST)
                    await self.powerChanged(p1, False)
            except Exception as err:
                _LOGGER.error(err)
                _LOGGER.error(traceback.format_exc())

    async def powerChanged(self, p1: int, isFast: bool) -> None:
        # get the current power
        actualHome = 0
        actualSolar = 0
        availEnergy = 0
        for d in self.devices:
            if await d.power_get():
                #if d.name == "SolarFlow 800":
                #    d.actualSolar = 80
                actualHome += d.actualHome
                actualSolar += d.actualSolar
                availEnergy += d.availableKwh.asNumber

        # Update the power entities
        power = actualHome + p1
        self.power.update_value(power)
        self.availableKwh.update_value(availEnergy)
        powerAverage = sum(self.power_history) // len(self.power_history) if len(self.power_history) > 0 else 0
        self.p1_powerAverage.update_value(powerAverage)

        # Callback to update power distribution
        async def powerUpdate(power: int, zero: bool = False) -> None:
            _LOGGER.info(f"Update power distribution => {0 if zero else power}W average {powerAverage}W")
            self.power_history.append(power)
            if power == 0 or zero:

                self.solardischarge.update_value(False)
                self.batterydischarge.update_value(False)
                self.batterycharge.update_value(False)

                for d in self.devices:
                    await d.power_discharge(0)
            elif power < -SmartMode.START_POWER:

                self.solardischarge.update_value(False)
                self.batterydischarge.update_value(False)
                self.batterycharge.update_value(True)

                await self.powerCharge(powerAverage, power)
            elif power <= actualSolar:

                self.solardischarge.update_value(True)
                self.batterydischarge.update_value(False)
                self.batterycharge.update_value(False)

                for d in self.devices:
                    d.deivicestate_active.update_value(False)
                    d.deivicestate_inaktive.update_value(False)
                    d.deivicestate_offline.update_value(False)
                    d.deivicestate_starting.update_value(False)
                    d.deivicestate_socfull.update_value(False)

                # excess solar power, only discharge
                _LOGGER.info(f"powerSolar => {power}W average {powerAverage}W")
                for d in sorted(self.devices, key=lambda d: d.actualKwh + d.activeKwh, reverse=True):
                    if d.online:
                        d.activeKwh = 0.5 if power > 0 else 0
                        pwr = min(d.actualSolar, max(0, power))
                        power -= await d.power_discharge(pwr)
            else:

                self.solardischarge.update_value(False)
                self.batterydischarge.update_value(True)
                self.batterycharge.update_value(False)

                await self.powerDischarge(powerAverage, power, actualSolar)

        # Update power distribution.
        _LOGGER.info(f"P1 ======> p1:{p1} isFast:{isFast}, home:{actualHome}W, solar:{actualSolar}W")
        match self.operation:
            case SmartMode.MATCHING:
                if (powerAverage > 0 and power >= 0) or (powerAverage < 0 and power <= 0):
                    await powerUpdate(power)
                else:
                    await powerUpdate(power, True)

            case SmartMode.MATCHING_DISCHARGE:
                await powerUpdate(max(actualSolar, power))

            case SmartMode.MATCHING_CHARGE:
                await powerUpdate(min(actualSolar, power))

            case SmartMode.MANUAL:
                await powerUpdate(int(self.manualpower.asNumber))

    async def powerCharge(self, average: int, power: int) -> None:
        totalKwh = 0.0
        totalMin = 0
        total = average
        starting = average
        count = 0
        _LOGGER.info(f"powerCharge => {power}W average {average}W")

        self.devices = sorted(self.devices, key=lambda d: d.actualKwh + d.activeKwh, reverse=False)

        for d in self.devices:
            start = d.startCharge if d.actualHome == 0 else d.minCharge
            if d.state in (DeviceState.INACTIVE, DeviceState.SOCEMPTY) and (totalMin == 0 or total < start):
                if not d.fuseCharge(d):
                    continue
                if d.actualHome < 0:
                    d.state = DeviceState.ACTIVE
                    d.activeKwh = -SmartMode.KWHSTEP
                    totalKwh += d.actualKwh
                    totalMin += d.minCharge
                    total -= d.startCharge
                    count += 1
                elif totalMin == 0 or starting < start:
                    d.state = DeviceState.STARTING
                    d.activeKwh = -SmartMode.KWHSTEP
                    totalMin += 1
                starting -= d.startCharge

        flexPwr = max(power, power - totalMin)
        for d in self.devices:
            match d.state:
                case DeviceState.ACTIVE:
                    if count == 1:
                        power -= await d.power_charge(min(0, power))
                    else:
                        pwr = max(d.maxCharge - d.minCharge, int(flexPwr * (2 / count - d.actualKwh / totalKwh if totalKwh > 0 else 1)))
                        flexPwr -= pwr
                        totalKwh -= d.actualKwh
                        pwr = d.minCharge + pwr
                        power -= await d.power_charge(min(max(power, pwr), 0))
                        count -= 1
                case DeviceState.STARTING:
                    await d.power_charge(min(0, -SmartMode.STARTWATT - d.actualSolar))
                case DeviceState.OFFLINE:
                    continue
                case _:
                    d.activeKwh = 0
                    await d.power_discharge(d.actualSolar)
        _LOGGER.info(f"powerCharge => left {power}W")

    async def powerDischarge(self, average: int, power: int, solar: int) -> None:
        starting = average - solar
        total = average - solar
        totalMin = 0
        totalWeight = 0.0

        def sortDevices(d: ZendureDevice) -> float:
            d.maxDischarge = max(0, d.limitDischarge - d.actualSolar)
            self.total += d.maxDischarge
            return d.actualKwh + d.activeKwh

        self.total = 0
        self.devices = sorted(self.devices, key=sortDevices, reverse=True)

        _LOGGER.info(f"powerDischarge => {power}W average {average}W, total {self.total}W")
        self.total = 0

        batteryout = 0

        for d in self.devices:
            batteryout += d.batteryOutput.asInt

        for d in self.devices:
            _LOGGER.info(f"state => {d.state} actualhome{d.actualHome} battout {batteryout}")
            start = d.startDischarge if d.batteryOutput.asInt == 0 else d.minDischarge
            if d.state in (DeviceState.INACTIVE, DeviceState.SOCFULL) and ((totalMin == 0 or total > start) or (d.state == DeviceState.SOCFULL and d.actualHome == 0 and batteryout > 0)) and not d.MinSoCWindow:
                _LOGGER.info(f"First Round => {d.name} state: {d.state}")
                if not d.fuseDischarge(d):
                    continue
                if d.batteryOutput.asInt > 0 or (d.state == DeviceState.SOCFULL and d.actualHome > 0):
                    d.state = DeviceState.ACTIVE
                    d.activeKwh = SmartMode.KWHSTEP
                    total -= d.startDischarge
                    totalMin += d.minDischarge
                    totalWeight += d.actualKwh * d.maxDischarge
                    self.total += d.maxDischarge
                elif (totalMin == 0 and starting > SmartMode.START_POWER) or starting > start or (d.state == DeviceState.SOCFULL and d.actualHome == 0 and batteryout > 0):
                    d.state = DeviceState.STARTING
                    d.activeKwh = SmartMode.KWHSTEP
                    totalMin += 1

                starting -= d.startDischarge

        flexPwr = max(0, power - totalMin - solar)

        for d in self.devices:
            _LOGGER.info(f"Second Round => {d.name} state: {d.state}")
            match d.state:
                case DeviceState.ACTIVE:
                    d.deivicestate_active.update_value(True)
                    d.deivicestate_inaktive.update_value(False)
                    d.deivicestate_offline.update_value(False)
                    d.deivicestate_starting.update_value(False)
                    d.deivicestate_socfull.update_value(False)
                    pwr = min(d.maxDischarge - d.minDischarge, int(flexPwr * (d.maxDischarge * d.actualKwh / totalWeight if totalWeight > 0 else 0)))
                    flexPwr -= pwr
                    totalWeight -= d.maxDischarge * d.actualKwh
                    pwr = d.minDischarge + pwr
                    power -= await d.power_discharge(min(power, pwr + d.actualSolar))
                case DeviceState.STARTING:
                    d.deivicestate_active.update_value(False)
                    d.deivicestate_inaktive.update_value(False)
                    d.deivicestate_offline.update_value(False)
                    d.deivicestate_starting.update_value(True)
                    d.deivicestate_socfull.update_value(False)
                    power -= await d.power_discharge(SmartMode.STARTWATT + d.actualSolar) - SmartMode.STARTWATT
                case DeviceState.OFFLINE:
                    d.deivicestate_active.update_value(False)
                    d.deivicestate_inaktive.update_value(False)
                    d.deivicestate_offline.update_value(True)
                    d.deivicestate_starting.update_value(False)
                    d.deivicestate_socfull.update_value(False)
                    continue
                case DeviceState.INACTIVE:
                    d.deivicestate_active.update_value(False)
                    d.deivicestate_inaktive.update_value(True)
                    d.deivicestate_offline.update_value(False)
                    d.deivicestate_starting.update_value(False)
                    d.deivicestate_socfull.update_value(False)
                    d.activeKwh = 0
                    power -= await d.power_discharge(d.actualSolar)
                case DeviceState.SOCFULL:
                    if batteryout > 0 and d.actualHome > 0:
                        d.deivicestate_active.update_value(False)
                        d.deivicestate_inaktive.update_value(False)
                        d.deivicestate_offline.update_value(False)
                        d.deivicestate_starting.update_value(False)
                        d.deivicestate_socfull.update_value(True)
                        d.activeKwh = 0
                        power -= await d.power_discharge(d.actualSolar)
                
        _LOGGER.info(f"powerDischarge => left {power}W")

    def update_fusegroups(self) -> None:
        _LOGGER.info("Update fusegroups")

        # updateFuseGroup callback
        def updateFuseGroup(_entity: ZendureRestoreSelect, _value: Any) -> None:
            self.update_fusegroups()

        fuseGroups: dict[str, FuseGroup] = {}
        for device in self.devices:
            try:
                if device.fuseGroup.onchanged is None:
                    device.fuseGroup.onchanged = updateFuseGroup

                match device.fuseGroup.state:
                    case "owncircuit" | "group3600":
                        fg = FuseGroup(device.name, 3600, -3600)
                    case "group800":
                        fg = FuseGroup(device.name, 800, -1200)
                    case "group1200":
                        fg = FuseGroup(device.name, 1200, -1200)
                    case "group2000":
                        fg = FuseGroup(device.name, 2000, -2000)
                    case "group2400":
                        fg = FuseGroup(device.name, 2400, -2400)
                    case _:
                        continue

                fg.devices.append(device)
                fuseGroups[device.deviceId] = fg
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
                for deviceId, fg in fuseGroups.items():
                    if deviceId != device.deviceId:
                        fusegroups[deviceId] = f"Part of {fg.name} fusegroup"
                device.fuseGroup.setDict(fusegroups)
            except:  # noqa: E722
                _LOGGER.error(f"Unable to create fusegroup for device: {device.name} ({device.deviceId})")

        # Add devices to fusegroups
        for device in self.devices:
            if fg := fuseGroups.get(device.fuseGroup.value):
                fg.devices.append(device)
            device.setStatus()

        # check if we can split fuse groups
        self.fuseGroups.clear()
        for fg in fuseGroups.values():
            if len(fg.devices) > 1 and fg.maxpower >= sum(d.limitDischarge for d in fg.devices) and fg.minpower <= sum(d.limitCharge for d in fg.devices):
                for d in fg.devices:
                    self.fuseGroups.append(FuseGroup(d.name, d.limitDischarge, d.limitCharge, [d]))
            else:
                for d in fg.devices:
                    d.fuseCharge = fg.fuseCharge
                    d.fuseDischarge = fg.fuseDischarge
                self.fuseGroups.append(fg)

    async def update_operation(self, entity: ZendureSelect, _operation: Any) -> None:
        operation = int(entity.value)
        _LOGGER.info(f"Update operation: {operation} from: {self.operation}")

        self.operation = operation
        self.power_history.clear()
        if self.p1meterEvent is not None:
            if operation != SmartMode.NONE and (len(self.devices) == 0 or all(not d.online for d in self.devices)):
                _LOGGER.warning("No devices online, not possible to start the operation")
                persistent_notification.async_create(self.hass, "No devices online, not possible to start the operation", "Zendure", "zendure_ha")
                return

            match self.operation:
                case SmartMode.NONE:
                    self.solardischarge.update_value(False)
                    self.batterydischarge.update_value(False)
                    for d in self.devices:
                        d.deivicestate_active.update_value(False)
                        d.deivicestate_inaktive.update_value(False)
                        d.deivicestate_offline.update_value(False)
                        d.deivicestate_starting.update_value(False)
                    d.deivicestate_socfull.update_value(False)
                    if len(self.devices) > 0:
                        for d in self.devices:
                            await d.power_off()
