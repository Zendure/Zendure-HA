"""Coordinator for Zendure integration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import traceback
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from math import sqrt
from pathlib import Path
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
from .device import DeviceSettings, ZendureDevice, ZendureLegacy
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
    simulation: bool = False

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
        self.pwr_total = 0
        self.pwr_avg = 0
        self.pwr_count = 0
        self.pwr_update = 0
        # Track mode changes to prevent oscillation
        self.last_mode: str = "idle"  # "idle", "charging", "discharging"
        self.last_mode_change: datetime = datetime.min
        self.p1meterEvent: Callable[[], None] | None = None
        self.p1_unit_logged: bool = False  # Track if we've logged the P1 meter unit
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

        self.operationmode = (ZendureRestoreSelect(self, "Operation", {0: "off", 1: "manual", 2: "smart", 3: "smart_discharging", 4: "smart_charging"}, self.update_operation),)
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

        self.devices = list(Api.devices.values())
        _LOGGER.info(f"Loaded {len(self.devices)} devices")

        # initialize the api & p1 meter
        await EntityDevice.add_entities()
        self.api.Init(self.config_entry.data, mqtt)
        self.update_p1meter(self.config_entry.data.get(CONF_P1METER, "sensor.power_actual"))
        await asyncio.sleep(1)  # allow other tasks to run
        self.update_fusegroups()
        Api.mqttLogging = True

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

                fg: FuseGroup | None = None
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
                        _LOGGER.debug("Device %s has unsupported fuseGroup state: %s", device.name, device.fuseGroup.state)
                        continue

                if fg is not None:
                    fg.devices.append(device)
                    fuseGroups[device.deviceId] = fg
            except AttributeError as err:
                _LOGGER.error("Device %s missing fuseGroup attribute: %s", device.name, err)
            except Exception as err:
                _LOGGER.error("Unable to create fusegroup for device %s (%s): %s", device.name, device.deviceId, err, exc_info=True)

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
            except AttributeError as err:
                _LOGGER.error("Device %s missing fuseGroup attribute: %s", device.name, err)
            except Exception as err:
                _LOGGER.error("Unable to update fusegroup options for device %s (%s): %s", device.name, device.deviceId, err, exc_info=True)

        # Add devices to fusegroups
        for device in self.devices:
            if fg := fuseGroups.get(device.fuseGroup.value):
                device.fuseGrp = fg
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

    def writeSimulation(self, time: datetime, p1: int) -> None:
        if Path("simulation.csv").exists() is False:
            with Path("simulation.csv").open("w") as f:
                f.write(
                    "Time;P1;Operation;Battery;Solar;Home;--;"
                    + ";".join(
                        [
                            f"bat;Prod;Home;{
                                json.dumps(
                                    DeviceSettings(
                                        d.name,
                                        d.fuseGrp.name,
                                        d.limitCharge,
                                        d.limitDischarge,
                                        d.maxSolar,
                                        d.kWh,
                                        d.socSet.asNumber,
                                        d.minSoc.asNumber,
                                    ),
                                    default=vars,
                                )
                            }"
                            for d in self.devices
                        ]
                    )
                    + "\n"
                )

        with Path("simulation.csv").open("a") as f:
            data = ""
            tbattery = 0
            tsolar = 0
            thome = 0

            for d in self.devices:
                tbattery += (pwr_battery := d.batteryOutput.asInt - d.batteryInput.asInt)
                tsolar += (pwr_solar := d.solarInput.asInt)
                thome += (pwr_home := d.homeOutput.asInt - d.homeInput.asInt)
                data += f";{pwr_battery};{pwr_solar};{pwr_home};{d.electricLevel.asInt}"

            f.write(f"{time};{p1};{self.operation};{tbattery};{tsolar};{thome};" + data + "\n")

    @callback
    async def _p1_changed(self, event: Event[EventStateChangedData]) -> None:
        # update new entities
        await EntityDevice.add_entities()

        # exit if there is nothing to do
        if not self.hass.is_running or (new_state := event.data["new_state"]) is None:
            return

        try:  # convert the state to a float
            p1_raw = float(new_state.state)
        except ValueError:
            return

        # Get unit of measurement and convert to Watts if necessary
        unit = new_state.attributes.get("unit_of_measurement", "W")
        
        # Log the unit once for debugging
        if not self.p1_unit_logged:
            _LOGGER.info("P1 meter unit detected: %s (sensor: %s)", unit, new_state.entity_id)
            self.p1_unit_logged = True
        
        # Convert to Watts based on unit
        if unit in ("kW", "kilowatt", "kilowatts"):
            p1 = int(p1_raw * 1000)  # Convert kW to W
            _LOGGER.debug("Converted P1 from %.3f kW to %d W", p1_raw, p1)
        elif unit in ("W", "watt", "watts", ""):
            p1 = int(p1_raw)  # Already in Watts
        else:
            _LOGGER.warning("Unknown P1 meter unit '%s', assuming Watts. Please check your P1 sensor configuration.", unit)
            p1 = int(p1_raw)

        # Get time & update simulation
        time = datetime.now()
        if ZendureManager.simulation:
            self.writeSimulation(time, p1)

        # Check for fast delay
        if time < self.zero_fast:
            self.p1_history.append(p1)
            return

        # calculate the standard deviation
        if len(self.p1_history) > 1:
            avg = int(sum(self.p1_history) / len(self.p1_history))
            stddev = SmartMode.Threshold * max(SmartMode.MAX_STDDEV_THRESHOLD, sqrt(sum([pow(i - avg, 2) for i in self.p1_history]) / len(self.p1_history)))
            if isFast := abs(p1 - avg) > stddev or abs(p1 - self.p1_history[0]) > stddev:
                self.p1_history.clear()
        else:
            isFast = False
        self.p1_history.append(p1)

        # check minimal time between updates
        if isFast or time > self.zero_next:
            try:
                self.zero_next = time + timedelta(seconds=SmartMode.TIMEZERO)
                self.zero_fast = time + timedelta(seconds=SmartMode.TIMEFAST)
                await self.powerChanged(p1, isFast)
            except Exception as err:
                _LOGGER.error(err)
                _LOGGER.error(traceback.format_exc())

    async def powerChanged(self, p1: int, isFast: bool) -> None:
        # get the current power
        availEnergy = 0
        pwr_bypass = 0
        pwr_home = 0
        pwr_produced = 0

        devices: list[ZendureDevice] = []
        for d in self.devices:
            if await d.power_get():
                availEnergy += d.availableKwh.asNumber
                pwr_bypass += -d.pwr_home if d.state == DeviceState.SOCFULL else 0
                pwr_home += d.pwr_home
                pwr_produced += d.pwr_produced
                devices.append(d)

        # Get the setpoint
        pwr_setpoint = pwr_home + p1
        if issurplus := self.operation == SmartMode.MATCHING and pwr_setpoint > 0 and abs(pwr_bypass) > pwr_setpoint:
            pwr_setpoint += pwr_bypass
        elif pwr_setpoint < 0 and pwr_setpoint < pwr_produced + pwr_bypass:
            pwr_setpoint += pwr_produced

        # Update the power entities
        self.power.update_value(pwr_home + pwr_produced)
        self.availableKwh.update_value(availEnergy)
        self.pwr_update += 1
        self.pwr_total = 0
        self.pwr_count = 0
        self.pwr_avg = 0

        # reset history on fast change and discharging
        if len(self.power_history) > 1:
            avg = int(sum(self.power_history) / len(self.power_history))
            if avg > 0 and pwr_setpoint < 0:
                self.power_history.clear()
            else:
                stddev = SmartMode.ThresholdAvg * max(SmartMode.MAX_STDDEV_THRESHOLD_AVG, sqrt(sum([pow(i - avg, 2) for i in self.power_history]) / len(self.power_history)))
                if abs(pwr_setpoint - avg) > stddev or abs(pwr_setpoint - self.power_history[0]) > stddev:
                    self.power_history.clear()

        self.power_history.append(pwr_setpoint)
        p1_average = sum(self.power_history) // len(self.power_history)

        # Determine target mode to detect mode changes
        current_mode = "idle"
        if pwr_setpoint > SmartMode.IGNORE_DELTA:
            current_mode = "discharging"
        elif pwr_setpoint < -SmartMode.IGNORE_DELTA:
            current_mode = "charging"

        # Hysteresis: prevent rapid mode switching
        time_since_last_change = (time - self.last_mode_change).total_seconds()
        if current_mode != self.last_mode and time_since_last_change < SmartMode.MIN_SWITCH_INTERVAL:
            _LOGGER.info(
                "Preventing mode switch from %s to %s (only %d seconds since last change, minimum is %d)",
                self.last_mode,
                current_mode,
                int(time_since_last_change),
                SmartMode.MIN_SWITCH_INTERVAL,
            )
            # Stay in current mode - don't update devices
            return

        # Track mode changes
        if current_mode != self.last_mode:
            _LOGGER.info("Mode changed from %s to %s", self.last_mode, current_mode)
            self.last_mode = current_mode
            self.last_mode_change = time

        # Update power distribution.
        _LOGGER.info(f"P1 ======> p1:{p1} isFast:{isFast}, setpoint:{pwr_setpoint}W produced:{pwr_produced}W")
        match self.operation:
            case SmartMode.MATCHING:
                if pwr_setpoint >= 0:
                    await self.powerDischarge(devices, p1_average, pwr_setpoint)
                elif pwr_setpoint <= 0 and p1_average > 0:
                    await self.powerDischarge(devices, 0, 0)
                elif pwr_setpoint <= 0:
                    await self.powerCharge(devices, p1_average, pwr_setpoint, issurplus)

            case SmartMode.MATCHING_DISCHARGE:
                await self.powerDischarge(devices, p1_average, max(0, pwr_setpoint))

            case SmartMode.MATCHING_CHARGE:
                if pwr_setpoint > -SmartMode.STARTWATT and pwr_setpoint < -pwr_produced:
                    await self.powerDischarge(devices, p1_average, pwr_setpoint)
                else:
                    await self.powerCharge(devices, p1_average, min(0, pwr_setpoint), issurplus)

            case SmartMode.MANUAL:
                if (setpoint := int(self.manualpower.asNumber)) > 0:
                    await self.powerDischarge(devices, setpoint, setpoint)
                else:
                    await self.powerCharge(devices, setpoint, setpoint, True)

    async def powerCharge(self, devices: list[ZendureDevice], average: int, setpoint: int, issurplus: bool) -> None:
        def sortCharge(d: ZendureDevice) -> int:
            if d.state == DeviceState.SOCFULL:
                return 0
            d.pwr_load = d.limitCharge // 4
            if (d.homeOutput.asInt > 0 or d.batteryInput.asInt > 0) and d.state != DeviceState.SOCFULL:
                self.pwr_count += 1
                self.pwr_total += d.fuseGrp.chargePower(d, self.pwr_update)
            return d.electricLevel.asInt - (5 if d.batteryInput.asInt > SmartMode.STARTWATT else 0)

        devices.sort(key=sortCharge, reverse=False)
        _LOGGER.info(f"powerCharge => setpoint {setpoint} cnt {self.pwr_count}")

        # distribute the power over the devices
        isFirst = True
        setpoint = max(setpoint, self.pwr_total)
        for d in devices:
            if d.state == DeviceState.SOCFULL:
                await d.power_discharge(0)
            else:
                if (d.homeOutput.asInt > 0 or d.batteryInput.asInt > 0) and setpoint < 0:
                    if self.pwr_count > 1 and setpoint < d.pwr_load and self.pwr_total < 0:
                        pct = min(1, max(0.125, setpoint / self.pwr_total))
                        pct = pct if pct < 0.25 or pct > 0.8 else min(0.9, pct + min(0.2, self.pwr_count * 0.075))
                        pwr = max(int(pct * d.maxPower), setpoint)
                        self.pwr_count -= 1
                        self.pwr_total -= d.maxPower
                    else:
                        pwr = setpoint

                    if issurplus:
                        pwr = await d.power_discharge(pwr) if pwr > 0 else -d.homeOutput.asInt + await d.power_charge(pwr)
                    elif d.pwr_produced == 0:
                        pwr = await d.power_charge(pwr)
                    elif d.pwr_produced < pwr:
                        pwr = d.pwr_produced + await d.power_discharge(pwr - d.pwr_produced)
                    else:
                        pwr = d.pwr_produced + await d.power_charge(pwr - d.pwr_produced)

                    setpoint = min(0, setpoint - pwr)

                elif average < d.pwr_load or isFirst:
                    await d.power_discharge(SmartMode.STARTWATT) if d.pwr_produced < -SmartMode.STARTWATT else await d.power_charge(-SmartMode.STARTWATT)
                else:
                    await d.power_discharge(0 if issurplus else -d.pwr_produced)
                average -= d.pwr_load
                isFirst = False

        # Distribution done, remaining power should be zero
        if setpoint != 0:
            _LOGGER.info(f"powerDistribution => left {setpoint}W")

    async def powerDischarge(self, devices: list[ZendureDevice], average: int, setpoint: int) -> None:
        def sortDischarge(d: ZendureDevice) -> int:
            if d.state == DeviceState.SOCEMPTY:
                return 101
            d.pwr_load = d.limitDischarge // 4
            if d.homeOutput.asInt > 0:
                self.pwr_count += 1
                self.pwr_total += d.fuseGrp.dischargePower(d, self.pwr_update)
            return d.electricLevel.asInt + (5 if d.homeOutput.asInt > SmartMode.STARTWATT else 0)

        devices.sort(key=sortDischarge, reverse=True)
        _LOGGER.info(f"powerDischarge => setpoint {setpoint} cnt {self.pwr_count}")

        # distribute the power over the devices
        isFirst = True
        setpoint = min(setpoint, self.pwr_total)
        for d in devices:
            if d.state == DeviceState.SOCEMPTY:
                await d.power_discharge(-d.pwr_produced)
            else:
                if d.homeOutput.asInt > 0 and setpoint > 0:
                    if self.pwr_count > 1 and setpoint > d.pwr_load and self.pwr_total > 0:
                        pct = min(1, max(0.125, setpoint / self.pwr_total))
                        pct = pct if pct < 0.25 or pct > 0.8 else min(0.9, pct + self.pwr_count * 0.05)
                        pwr = min(int(pct * d.maxPower), setpoint)
                        self.pwr_count -= 1
                        self.pwr_total -= d.maxPower
                    else:
                        pwr = setpoint

                    pwr = await d.power_discharge(pwr)
                    setpoint = max(0, setpoint - pwr)

                elif average > d.pwr_load or isFirst:
                    await d.power_discharge(SmartMode.STARTWATT)
                else:
                    await d.power_discharge(0)
                average -= d.pwr_load
                isFirst = False

        # Distribution done, remaining power should be zero
        if setpoint != 0:
            _LOGGER.info(f"powerDistribution => left {setpoint}W")
