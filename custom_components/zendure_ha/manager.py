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
from .power_distribution import MainState, SubState, decide_substate, distribute_power

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

        self._last_allocation: dict[ZendureDevice, int] = {}
        self._starting_device: ZendureDevice | None = None

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
        """Entscheide MainState und verteile Leistung auf Devices."""
        # get the current power
        availEnergy = 0
        pwr_home = 0
        pwr_battery = 0
        pwr_solar = 0
        devices: list[ZendureDevice] = []
        for d in self.devices:
            if await d.power_get():
                availEnergy += d.availableKwh.asNumber
                pwr_home += (d.pwr_home_out if not d.is_bypass else 0) - d.pwr_home_in
                pwr_battery += d.pwr_battery_out - d.pwr_battery_in
                pwr_solar += d.pwr_solar
                devices.append(d)

        # Update the power entities
        self.power.update_value(pwr_home)
        self.availableKwh.update_value(availEnergy)
        pwr_setpoint = pwr_home + p1
        self.power_history.append(pwr_setpoint)
        p1_average = sum(self.power_history) // len(self.power_history)

        # Update power distribution.
        match self.operation:
            case SmartMode.MATCHING:
                if (p1_average > 0 and pwr_setpoint >= 0) or (p1_average < 0 and pwr_setpoint <= 0):
                    await self.powerDistribution(devices, p1_average, pwr_setpoint, pwr_solar, isFast)
                else:
                    await self.powerDistribution(devices, p1_average, pwr_setpoint, pwr_solar, isFast)
                    #for d in devices:
                    #    if not d.is_bypass:
                    #        pwr_setpoint -= await d.power_discharge(max(0, min(pwr_setpoint, d.limitDischarge), d.pwr_solar))

            case SmartMode.MATCHING_DISCHARGE:
                await self.powerDistribution(devices, p1_average, max(0, pwr_setpoint), pwr_solar, isFast)

            case SmartMode.MATCHING_CHARGE:
                await self.powerDistribution(devices, p1_average, min(0, pwr_setpoint), pwr_solar, isFast)

            case SmartMode.MANUAL:
                await self.powerDistribution(devices, int(self.manualpower.asNumber), int(self.manualpower.asNumber), pwr_solar, isFast)

    async def powerDistribution(self, devices: list[ZendureDevice], power_to_devide_avg: int, power_to_devide: int, pwr_solar: int, isFast: bool) -> None:

        # Leistung bestimmen (Hauslast + Zählerstand)
        _LOGGER.info(f"PowerChanged: power_to_devide={power_to_devide}")

        # MainState entscheiden
        main_state = MainState.GRID_CHARGE if power_to_devide < 0 else MainState.GRID_DISCHARGE

        # Device-Daten holen und SubStates setzen
        active_devices = []
        for dev in self.devices:
            sub = decide_substate(dev, main_state)
            dev.state_machine.main = main_state
            dev.state_machine.sub = sub
            active_devices.append(dev)

        # Leistung verteilen
        allocation = distribute_power(active_devices, power_to_devide, main_state)

        # FuseGroup-Schutz: Allocation nach Sicherungslimits anpassen
        for fg in self.fuseGroups:
            total_power = sum(allocation.get(d, 0) for d in fg.devices)
            # Discharge-Limit prüfen
            if total_power > fg.maxpower:
                factor = fg.maxpower / total_power
                for d in fg.devices:
                    if d in allocation:
                        allocation[d] = int(allocation[d] * factor)
                _LOGGER.warning(f"FuseGroup {fg.name}: Begrenzung Discharge {total_power}W -> {fg.maxpower}W")
            # Charge-Limit prüfen (falls du das auch absichern willst)
            if total_power < fg.minpower:
                factor = fg.minpower / total_power if total_power != 0 else 0
                for d in fg.devices:
                    if d in allocation:
                        allocation[d] = int(allocation[d] * factor)
                _LOGGER.warning(f"FuseGroup {fg.name}: Begrenzung Charge {total_power}W -> {fg.minpower}W")
                
        # Startgerät-Erkennung
        if self._starting_device is None and not isFast:
            for dev, new_power in allocation.items():
                last_power = self._last_allocation.get(dev, None)
                # Fall 1: Gerät neu drin
                if last_power is None and new_power > 0:
                    self._starting_device = dev
                    break
                # Fall 2: Gerät von 0 auf >0
                elif last_power == 0 and new_power > 0:
                    self._starting_device = dev
                    break

        # Kickstart-Phase
        if self._starting_device and not isFast:
            dev = self._starting_device
            if (main_state == MainState.GRID_DISCHARGE and dev.pwr_home_out > 0) or \
            (main_state == MainState.GRID_CHARGE and dev.pwr_home_in > 0):
                # Gerät hat bereits Output/Input Kickstart fertig
                _LOGGER.info(f"{dev.name} gestartet, Kickstart beendet.")
                self._starting_device = None
            else:
                # Kickstart nur an das Startgerät
                if main_state == MainState.GRID_DISCHARGE:
                    await dev.power_discharge(min(dev.limitDischarge, 50))
                else:
                    await dev.power_charge(-50)
                _LOGGER.info(f"Kickstart für {dev.name}: 50W")
                return  # WICHTIG: keine weiteren Geräte in dieser Runde ansteuern
        elif self._starting_device and isFast:
            self._starting_device = None

        # Normale Allocation schicken
        for dev, power in allocation.items():
            if main_state == MainState.GRID_DISCHARGE:
                await dev.power_discharge(min(dev.limitDischarge, power))
                _LOGGER.info(f"Discharge={dev.name} power: {power}")
            else:
                await dev.power_charge(-power)
                _LOGGER.info(f"Charge={dev.name} power: {-power}")

        # Allocation Speichern für nächste Runde
        self._last_allocation = allocation.copy()
        _LOGGER.info(f"Verteilung abgeschlossen: {allocation}") 

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
