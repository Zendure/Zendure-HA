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
from homeassistant.components import bluetooth, persistent_notification
from homeassistant.components.number import NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.loader import async_get_integration

from .api import Api
from .const import CONF_P1METER, DOMAIN, DeviceState, ManagerState, SmartMode
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
    fuseGroups: list[FuseGroup] = []

    def __init__(self, hass: HomeAssistant, entry: ZendureConfigEntry) -> None:
        """Initialize Zendure Manager."""
        super().__init__(hass, _LOGGER, name="Zendure Manager", update_interval=SCAN_INTERVAL, config_entry=entry)
        EntityDevice.__init__(self, hass, "manager", "Zendure Manager", "Zendure Manager")
        self.operation = 0
        self.last_delta: float = SmartMode.TIMEIDLE
        self.last_discharge = datetime.max
        self.mode_idle = datetime.min
        self.zero_next = datetime.min
        self.zero_fast = datetime.min
        self.check_reset = datetime.min
        self.state = ManagerState.IDLE
        self.zorder: deque[int] = deque([25, -25], maxlen=8)
        self.p1meterEvent: Callable[[], None] | None = None
        self.api = Api()
        self.update_count = 0
        self.active_count = 0

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

        # calculate the standard deviation
        avg = int(sum(self.zorder) / len(self.zorder)) if len(self.zorder) > 1 else p1
        stddev = min(50, sqrt(sum([pow(i - avg, 2) for i in self.zorder]) / len(self.zorder)))
        if isFast := (len(self.zorder) > 1 and abs(p1 - avg) > SmartMode.Threshold * stddev):
            self.zorder.clear()
        self.zorder.append(p1)

        # check minimal time between updates
        time = datetime.now()
        if time > self.zero_fast and (isFast or time > self.zero_next):
            try:
                self.zero_next = time + timedelta(seconds=SmartMode.TIMEZERO)
                self.zero_fast = time + timedelta(seconds=SmartMode.TIMEFAST)

                self.active_count += 1
                if self.active_count > 1:
                    _LOGGER.info("==========> Skip p1 meter event, already active")
                    return
                _LOGGER.info(f"P1 meter changed => {p1}W, avg: {avg:.1f}W fast: {isFast}, stddev: {stddev:.1f}W")
                await self.powerChanged(p1 if isFast else avg, time)
            except Exception as err:
                _LOGGER.error(err)
                _LOGGER.error(traceback.format_exc())
            finally:
                self.active_count -= 1

    async def powerChanged(self, p1: int, time: datetime) -> None:
        # get the current power
        powerDischarge = 0
        powerCharge = 0
        powerSolar = 0
        powerHome = 0
        availEnergy = 0
        for d in self.devices:
            if await d.power_get():
                powerDischarge += d.packInputPower.asInt
                powerCharge += d.gridInputPower.asInt
                powerSolar += d.solarInputPower.asInt if d.useSolar and not d.byPass.is_on else 0
                powerHome += d.outputHomePower.asInt
                availEnergy += d.availableKwh.asNumber

        if any(d.state == DeviceState.STARTING for d in self.devices):
            for d in self.devices:
                if d.state == DeviceState.STARTING:
                    _LOGGER.info(f"Starting device {d.name} for discharge")

        powerActual = powerCharge + powerHome
        self.power.update_value(powerActual)
        self.availableKwh.update_value(availEnergy)
        _LOGGER.info(
            f"P1 power changed => {p1}W, discharge: {powerDischarge}W, charge: {powerCharge}W, solar: {powerSolar}W, home: {powerHome}W, actual: {powerActual}W"
        )

        # Update the power the power distribution.
        power = powerActual + p1
        match self.operation:
            case SmartMode.MATCHING:
                if power > powerSolar:
                    if self.state != ManagerState.DISCHARGING:
                        self.state = ManagerState.DISCHARGING
                        self.last_delta = SmartMode.TIMEIDLE + max(0, min((time - self.last_discharge).total_seconds(), SmartMode.TIMERESET))
                    self.last_discharge = time

                elif power < 0:
                    if self.state == ManagerState.DISCHARGING:
                        self.state = ManagerState.IDLE
                        self.mode_idle = time + timedelta(seconds=self.last_delta)

                    elif self.state == ManagerState.IDLE:
                        if self.mode_idle < time and abs(p1) > SmartMode.MIN_POWER:
                            self.state = ManagerState.CHARGING
                        else:
                            power = -powerSolar

                await self.powerUpdate(power, powerSolar)

            case SmartMode.MATCHING_DISCHARGE:
                await self.powerUpdate(max(powerSolar, power), powerSolar)

            case SmartMode.MATCHING_CHARGE:
                await self.powerUpdate(min(powerSolar, power), powerSolar)

            case SmartMode.MANUAL:
                await self.powerUpdate(int(self.manualpower.asNumber), powerSolar)

    async def powerUpdate(self, power: int, solar: int) -> None:
        if power == 0:
            for i in self.devices:
                i.power_discharge(0)
        elif power < -SmartMode.MIN_POWER:
            self.powerCharge(power)
        elif power > SmartMode.MIN_POWER + solar:
            self.powerDischarge(power)
        else:
            # Solar only output to home
            _LOGGER.info(f"Power update => {power} with solar only")
            for d in sorted(self.devices, key=lambda d: d.solarInputPower.asInt):
                if d.online and (sp := d.solarInputPower.asInt) > 0 and d.useSolar:
                    power -= d.power_discharge(min(sp, max(0, power)))
                else:
                    d.power_discharge(0)

    def powerCharge(self, power: int) -> None:
        totalKwh = 0.0
        totalMin = 0
        total = power - SmartMode.STARTWATT
        starting = power - SmartMode.STARTWATT
        count = 0
        self.devices = sorted(self.devices, key=lambda d: d.actualKwh + d.activeKwh, reverse=False)

        _LOGGER.info(f"powerCharge => {power}W")
        for d in self.devices:
            start = d.startCharge if d.actualWatt == 0 else d.minCharge
            if d.state in (DeviceState.INACTIVE, DeviceState.SOCEMPTY) and (totalMin == 0 or total < start) and d.kWh > 0:
                if d.actualWatt < 0:
                    d.state = DeviceState.ACTIVE
                    totalKwh += d.actualKwh
                    totalMin += d.minCharge
                    total -= d.startCharge
                    count += 1
                elif starting < start:
                    d.state = DeviceState.STARTING
                    d.activeKwh = -SmartMode.KWHSTEP
                    starting = False
                else:
                    d.activeKwh = 0
                starting -= d.startCharge

        # Distribute available power over fuse groups
        for fg in self.fuseGroups:
            fg.distribute(True)

        flexPwr = power - totalMin
        for d in self.devices:
            match d.state:
                case DeviceState.ACTIVE:
                    if count == 1:
                        power -= d.power_charge(power)
                    else:
                        pwr = max(d.maxCharge - d.minCharge, int(flexPwr * (2 / count - d.actualKwh / totalKwh if totalKwh > 0 else 1)))
                        flexPwr -= pwr
                        totalKwh -= d.actualKwh
                        pwr = d.minCharge + pwr
                        power -= d.power_charge(max(power, pwr))
                        count -= 1
                    _LOGGER.info(f"Charging {d.name} with {pwr}W, left {power}W")
                case DeviceState.STARTING:
                    d.power_charge(-SmartMode.STARTWATT)
                    _LOGGER.info(f"Starting charge {d.name} with {SmartMode.STARTWATT}W")
                case DeviceState.OFFLINE:
                    continue
                case _:
                    d.power_discharge(0)

    def powerDischarge(self, power: int) -> None:
        totalKwh = 0.0
        totalMin = 0
        total = power + SmartMode.STARTWATT
        starting = power + SmartMode.STARTWATT

        _LOGGER.info(f"powerDischarge => {power}W")
        self.devices = sorted(self.devices, key=lambda d: d.actualKwh + d.activeKwh, reverse=True)

        for d in self.devices:
            start = d.startDischarge if d.actualWatt == 0 else d.minDischarge
            if d.state in (DeviceState.INACTIVE, DeviceState.SOCFULL) and (totalMin == 0 or total > start) and d.kWh > 0:
                if d.actualWatt > 0:
                    d.state = DeviceState.ACTIVE
                    totalKwh += d.actualKwh
                    totalMin += d.minDischarge
                    total -= d.startDischarge
                elif starting > start:
                    d.state = DeviceState.STARTING
                    d.activeKwh = SmartMode.KWHSTEP
                starting -= d.startDischarge

        # Distribute available power over fuse groups
        for fg in self.fuseGroups:
            fg.distribute(False)

        total = sum(d.maxDischarge * d.actualKwh for d in self.devices if d.state == DeviceState.ACTIVE)
        flexPwr = power - totalMin
        for d in self.devices:
            match d.state:
                case DeviceState.ACTIVE:
                    pwr = min(d.maxDischarge - d.minDischarge, int(flexPwr * (d.maxDischarge * d.actualKwh / total if total > 0 else 0)))
                    flexPwr -= pwr
                    total -= d.maxDischarge * d.actualKwh
                    pwr = d.minDischarge + pwr
                    power -= d.power_discharge(min(power, pwr))
                    _LOGGER.info(f"Discharging {d.name} with {pwr}W, left {power}W")
                case DeviceState.STARTING:
                    d.power_discharge(SmartMode.STARTWATT)
                    _LOGGER.info(f"Starting discharge {d.name} with {SmartMode.STARTWATT}W")
                case DeviceState.OFFLINE:
                    continue
                case _:
                    d.power_discharge(0)

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
                        fusegroup = FuseGroup(device.name, 3600, -3600, device.maxDischarge, device.maxCharge)
                    case "group800":
                        fusegroup = FuseGroup(device.name, 800, -1200, device.maxDischarge, device.maxCharge)
                    case "group1200":
                        fusegroup = FuseGroup(device.name, 1200, -1200, device.maxDischarge, device.maxCharge)
                    case "group2000":
                        fusegroup = FuseGroup(device.name, 2000, -2000, device.maxDischarge, device.maxCharge)
                    case "group2400":
                        fusegroup = FuseGroup(device.name, 2400, -2400, device.maxDischarge, device.maxCharge)
                    case _:
                        continue

                fusegroup.devices.append(device)
                fuseGroups[device.deviceId] = fusegroup
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
                for deviceId, fusegroup in fuseGroups.items():
                    if deviceId != device.deviceId:
                        fusegroups[deviceId] = f"Part of {fusegroup.name} fusegroup"
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
        for fusegroup in fuseGroups.values():
            self.fuseGroups.append(fusegroup)

    def update_operation(self, entity: ZendureSelect, _operation: Any) -> None:
        operation = int(entity.value)
        _LOGGER.info(f"Update operation: {operation} from: {self.operation}")

        self.operation = operation
        if self.p1meterEvent is not None:
            if operation != SmartMode.NONE and (len(self.devices) == 0 or all(not d.online for d in self.devices)):
                _LOGGER.warning("No devices online, not possible to start the operation")
                persistent_notification.async_create(self.hass, "No devices online, not possible to start the operation", "Zendure", "zendure_ha")
                return

            match self.operation:
                case SmartMode.NONE:
                    if len(self.devices) > 0:
                        for d in self.devices:
                            d.power_off()
