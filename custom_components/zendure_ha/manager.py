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

SCAN_INTERVAL = timedelta(seconds=60)

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
        self.api = Api()
        self.operation = 0
        self.zero_next = datetime.min
        self.zero_fast = datetime.min
        self.check_reset = datetime.min
        self.power_history: deque[int] = deque(maxlen=25)
        self.p1_history: deque[int] = deque([25, -25], maxlen=8)
        self.pwr_load = 0
        self.pwr_max = 0
        self.p1meterEvent: Callable[[], None] | None = None
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
        self.manualpower = ZendureRestoreNumber(self, "manual_power", None, None, "W", "power", 10000, -10000, NumberMode.BOX, True)
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
            stddev = min(50, sqrt(sum([pow(i - avg, 2) for i in self.p1_history]) / len(self.p1_history)))
            if isFast := abs(p1 - avg) > SmartMode.Threshold * stddev:
                self.p1_history.clear()
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
        availEnergy = 0
        pwr_bypass = 0
        pwr_home = 0
        pwr_battery = 0
        pwr_solar = 0
        devices: list[ZendureDevice] = []
        for d in self.devices:
            if await d.power_get():
                availEnergy += d.availableKwh.asNumber
                pwr_bypass += d.homeOutput.asInt if d.state == DeviceState.SOCFULL else 0
                pwr_home += d.pwr_home
                pwr_battery += d.pwr_battery
                pwr_solar += d.pwr_solar
                devices.append(d)

        # Update the power entities
        self.power.update_value(pwr_home)
        self.availableKwh.update_value(availEnergy)
        pwr_setpoint = pwr_home + p1 - pwr_bypass
        if all(d.socLimit.asInt == 2 for d in self.devices):
            self.power_history.clear()
        self.power_history.append(pwr_setpoint)
        p1_average = sum(self.power_history) // len(self.power_history)

        # Update power distribution.
        _LOGGER.info(f"P1 ======> p1:{p1} isFast:{isFast}, home:{pwr_home}W solar:{pwr_solar}W bypass:{pwr_bypass}W")
        match self.operation:
            case SmartMode.MATCHING:
                if (p1_average > 0 and pwr_setpoint >= 0) or (p1_average < 0 and pwr_setpoint <= 0):
                    await self.powerDistribution(devices, p1_average, pwr_setpoint, pwr_solar)
                else:
                    for d in devices:
                        pwr_setpoint -= await d.power_discharge(max(0, pwr_setpoint, d.pwr_solar))

            case SmartMode.MATCHING_DISCHARGE:
                await self.powerDistribution(devices, p1_average, max(0, pwr_setpoint), pwr_solar)

            case SmartMode.MATCHING_CHARGE:
                await self.powerDistribution(devices, p1_average, min(0, pwr_setpoint), pwr_solar)

            case SmartMode.MANUAL:
                await self.powerDistribution(devices, int(self.manualpower.asNumber), int(self.manualpower.asNumber), pwr_solar)

    def strategyCharge(self, d: ZendureDevice) -> int:
        if d.state == DeviceState.SOCFULL:
            d.pwr_start = 0
            return 0
        d.state = DeviceState.INACTIVE
        d.pwr_start = d.limitCharge // (10 if d.pwr_active else 5)
        d.pwr_load = d.limitCharge // 6 if d.electricLevel.asInt > SmartMode.SOCMIN_OPTIMAL else int(d.limitCharge * 0.8)
        d.pwr_max = max(d.limitCharge, d.fuseGrp.maxCharge())
        self.pwr_load += d.limitCharge // 5
        self.pwr_max += d.pwr_max
        d.pwr_weight = int(100 * (d.actualKwh - (0.5 if d.pwr_active else 0)))
        return d.pwr_weight

    def strategySolar(self, d: ZendureDevice) -> int:
        if d.state == DeviceState.SOCEMPTY or (solar := d.solarInput.asInt) == 0:
            d.pwr_start = 0
            return 0
        d.state = DeviceState.INACTIVE
        d.pwr_start = min(solar, d.limitDischarge // (10 if d.pwr_active else 5))
        d.pwr_load = min(solar, d.limitDischarge // 5)
        d.pwr_max = min(solar, d.fuseGrp.maxDischarge())
        self.pwr_load += d.pwr_load
        self.pwr_max += d.pwr_max
        d.pwr_weight = int(100 * (d.actualKwh + (0.5 if d.pwr_active else 0)))
        return d.pwr_weight

    def strategyDischarge(self, d: ZendureDevice) -> int:
        if d.state == DeviceState.SOCEMPTY:
            d.pwr_start = 0
            return 0
        d.state = DeviceState.INACTIVE
        d.pwr_start = d.limitDischarge // (10 if d.pwr_active else 5)
        d.pwr_load = max(d.limitDischarge // 5, d.solarInput.asInt)
        d.pwr_max = min(d.limitDischarge, d.fuseGrp.maxDischarge())
        self.pwr_load += d.pwr_load
        self.pwr_max += d.pwr_max
        d.pwr_weight = int(100 * (d.actualKwh + (0.5 if d.pwr_active else 0)))
        return d.pwr_weight

    def inverseWeight(self, d: ZendureDevice, count: int, flexPwr: int, totalWeight: int) -> int:
        if flexPwr == 0 or totalWeight == 0 or count <= 1:
            pwr = flexPwr
        else:
            factor = (d.pwr_weight * d.pwr_max) / totalWeight
            pwr = int(flexPwr * (2 / count - factor))
        return max(d.pwr_max - d.pwr_load, pwr)

    def normalWeight(self, d: ZendureDevice, count: int, flexPwr: int, totalWeight: int) -> int:
        if flexPwr == 0 or totalWeight == 0 or count <= 1:
            pwr = flexPwr
        else:
            factor = (d.pwr_weight * d.pwr_max) / totalWeight
            pwr = int(flexPwr * factor)
        return min(d.pwr_max - d.pwr_load, pwr)

    async def powerDistribution(self, devices: list[ZendureDevice], p1_avg: int, p1_set: int, p1_solar: int) -> None:
        """Distribute power to devices based on current operation mode."""
        if p1_set < 0:
            # charge batteries
            distribute = self.inverseWeight
            strategy = self.strategyCharge
        elif p1_set < p1_solar:
            # solar only compensation
            distribute = self.normalWeight
            strategy = self.strategySolar
        else:
            # discharge batteries
            distribute = self.normalWeight
            strategy = self.strategyDischarge

        self.pwr_load = 0
        self.pwr_max = 0
        isCharging = p1_set < 0
        devices = sorted(devices, key=strategy, reverse=not isCharging)

        # determine which devices to use
        _LOGGER.info(f"powerDistribution => setp {p1_set} avg {p1_avg} {len(devices)} devices, load {self.pwr_load}W, max {self.pwr_max}W solar {p1_solar}W")
        count = 0
        totalPower = 0
        totalWeight = 0
        fixedPower = 0
        p1_starting = p1_avg
        for d in devices:
            d.pwr_active = False
            if d.pwr_start != 0 and (count == 0 or (d.pwr_start > p1_avg if isCharging else d.pwr_start < p1_avg)):
                if d.pwr_home < 0 if isCharging else d.pwr_home > 0:
                    d.state = DeviceState.ACTIVE
                    d.pwr_active = True
                    totalPower += d.fuseGrp.distribute(d, isCharging)
                    totalWeight += d.pwr_weight * d.pwr_max
                    p1_avg -= d.pwr_load
                    fixedPower += d.pwr_load
                    count += 1
                elif count == 0 or d.pwr_start > p1_starting if isCharging else d.pwr_start < p1_starting:
                    d.state = DeviceState.STARTING
                    d.pwr_active = True
                p1_starting -= d.pwr_load

        # update the power of the devices
        flexPower = min(0, p1_set - fixedPower) if isCharging else max(0, p1_set - fixedPower)
        for d in devices:
            match d.state:
                case DeviceState.ACTIVE:
                    pwr = distribute(d, count, flexPower, totalWeight)
                    flexPower -= pwr
                    p1_set -= await d.power_charge(max(p1_set, d.pwr_load + pwr)) if isCharging else await d.power_discharge(min(p1_set, d.pwr_load + pwr))
                    totalWeight -= d.pwr_weight * d.pwr_max
                    count -= 1
                case DeviceState.STARTING:
                    await d.power_charge(-SmartMode.STARTWATT) if isCharging else await d.power_discharge(SmartMode.STARTWATT)
                case _:
                    await d.power_discharge(0)

        # Distribution done, remaining power should be zero
        _LOGGER.info(f"powerDistribution => left {p1_set}W")

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
                        pwr = max(d.p1_min - d.minCharge, int(flexPwr * (2 / count - d.actualKwh / totalKwh if totalKwh > 0 else 1)))
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

    async def powerDischarge(self, average: int, power: int) -> None:
        starting = average
        total = average
        totalMin = 0
        totalWeight = 0.0

        def sortDevices(d: ZendureDevice) -> float:
            d.pwr_max = max(0, d.limitDischarge)
            self.total += d.pwr_max
            return d.actualKwh + d.activeKwh

        self.total = 0
        self.devices = sorted(self.devices, key=sortDevices, reverse=True)

        _LOGGER.info(f"powerDischarge => {power}W average {average}W, total {self.total}W")
        self.total = 0
        for d in self.devices:
            start = d.startDischarge if d.batteryOutput.asInt == 0 else d.minDischarge
            if d.state in (DeviceState.INACTIVE, DeviceState.SOCFULL) and (totalMin == 0 or total > start):
                if not d.fuseDischarge(d):
                    continue
                if d.batteryOutput.asInt > 0:
                    d.state = DeviceState.ACTIVE
                    d.activeKwh = SmartMode.KWHSTEP
                    total -= d.startDischarge
                    totalMin += d.minDischarge
                    totalWeight += d.actualKwh * d.pwr_max
                    self.total += d.pwr_max
                elif (totalMin == 0 and starting > SmartMode.START_POWER) or starting > start:
                    d.state = DeviceState.STARTING
                    d.activeKwh = SmartMode.KWHSTEP
                    totalMin += 1
                starting -= d.startDischarge

        flexPwr = max(0, power - totalMin)

        for d in self.devices:
            match d.state:
                case DeviceState.ACTIVE:
                    pwr = min(d.pwr_max - d.minDischarge, int(flexPwr * (d.pwr_max * d.actualKwh / totalWeight if totalWeight > 0 else 0)))
                    flexPwr -= pwr
                    totalWeight -= d.pwr_max * d.actualKwh
                    pwr = d.minDischarge + pwr
                    power -= await d.power_discharge(min(power, pwr + d.actualSolar))
                case DeviceState.STARTING:
                    power -= await d.power_discharge(SmartMode.STARTWATT + d.actualSolar) - SmartMode.STARTWATT
                case DeviceState.OFFLINE:
                    continue
                case _:
                    d.activeKwh = 0
                    power -= await d.power_discharge(min(power, d.actualSolar))
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
                    d.fuseGrp = fg
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
                    if len(self.devices) > 0:
                        for d in self.devices:
                            await d.power_off()
