"""Coordinator for Zendure integration."""

from __future__ import annotations

import logging
import traceback
from collections import deque
from datetime import datetime, timedelta
from math import sqrt

from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import ManagerMode, ManagerState, SmartMode
from .device import ZendureDevice

_LOGGER = logging.getLogger(__name__)


class Distribution:
    """Manage power distribution for Zendure devices."""

    def __init__(self, hass: HomeAssistant, p1meter: str) -> None:
        """Initialize Zendure Manager."""
        self.hass = hass
        self.p1_history: deque[int] = deque([25, -25], maxlen=8)
        self.p1_factor = 1
        self.devices: list[ZendureDevice] = []

        _LOGGER.debug("Updating P1 meter to: %s", p1meter)
        if self.p1meterEvent:
            self.p1meterEvent()
        if p1meter:
            self.p1meterEvent = async_track_state_change_event(self.hass, [p1meter], self._p1_changed)
            if (entity := self.hass.states.get(p1meter)) is not None and entity.attributes.get("unit_of_measurement", "W") in ("kW", "kilowatt", "kilowatts"):
                self.p1_factor = 1000
        else:
            self.p1meterEvent = None

    @callback
    async def _p1_changed(self, event: Event[EventStateChangedData]) -> None:
        # exit if there is nothing to do
        if not self.hass.is_running or not self.hass.is_running or (new_state := event.data["new_state"]) is None:
            return

        # convert the state to a float
        try:
            p1 = int(self.p1_factor * float(new_state.state))
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
            stddev = SmartMode.P1_STDDEV_FACTOR * max(SmartMode.P1_STDDEV_MIN, sqrt(sum([pow(i - avg, 2) for i in self.p1_history]) / len(self.p1_history)))
            if isFast := abs(p1 - avg) > stddev or abs(p1 - self.p1_history[0]) > stddev:
                self.p1_history.clear()
        else:
            isFast = False
        self.p1_history.append(p1)

        # check minimal time between updates
        if isFast or time > self.zero_next:
            try:
                # prevent updates during power distribution changes
                self.zero_fast = datetime.max
                await self.powerChanged(p1, isFast, time)
            except Exception as err:
                _LOGGER.error(err)
                _LOGGER.error(traceback.format_exc())

            time = datetime.now()
            self.zero_next = time + timedelta(seconds=SmartMode.TIMEZERO)
            self.zero_fast = time + timedelta(seconds=SmartMode.TIMEFAST)

    async def powerChanged(self, p1: int, isFast: bool, time: datetime) -> None:
        """Return the distribution setpoint."""
        availableKwh = 0
        setpoint = p1
        home = 0
        solar = 0
        offgrid = 0

        for d in self.devices:
            # get power production
            home += d.homePower.asInt
            solar += d.solarPower.asInt
            offgrid += d.offGrid.asInt if d.offGrid is not None else 0

            # only positive pwr_offgrid must be taken into account, negative values count a solarInput
            if (home := -d.homeInput.asInt + max(0, d.pwr_offgrid)) < 0:
                self.charge.append(d)
                self.charge_limit += d.fuseGrp.charge_limit(d)
                self.charge_optimal += d.charge_optimal
                self.charge_weight += d.pwr_max * (100 - d.electricLevel.asInt)
                setpoint += home

            elif (home := d.homeOutput.asInt) > 0 and d.state != DeviceState.SOCEMPTY:
                self.discharge.append(d)
                self.discharge_bypass -= d.pwr_produced if d.state == DeviceState.SOCFULL else 0
                self.discharge_limit += d.fuseGrp.discharge_limit(d)
                self.discharge_optimal += d.discharge_optimal
                self.discharge_produced -= d.pwr_produced
                self.discharge_weight += d.pwr_max * d.electricLevel.asInt
                setpoint += home

            else:
                self.idle.append(d)
                self.idle_lvlmax = max(self.idle_lvlmax, d.electricLevel.asInt)
                self.idle_lvlmin = min(self.idle_lvlmin, d.electricLevel.asInt if d.state != DeviceState.SOCFULL else 100)

            availableKwh += d.actualKwh
            power += d.pwr_offgrid + home + d.pwr_produced

        # Update the power entities
        self.power.update_value(power)
        self.availableKwh.update_value(availableKwh)
        if self.discharge_bypass > setpoint:
            setpoint -= self.discharge_bypass

        # Update power distribution.
        _LOGGER.info(f"P1 ======> p1:{p1} isFast:{isFast}, setpoint:{setpoint}W stored:{self.produced}W")
        match self.operation:
            case ManagerMode.MATCHING:
                if setpoint < 0:
                    await self.power_charge(setpoint, time)
                else:
                    await self.power_discharge(setpoint)

            case ManagerMode.MATCHING_DISCHARGE:
                # Only discharge, do nothing if setpoint is negative
                await self.power_discharge(max(0, setpoint))

            case ManagerMode.MATCHING_CHARGE:
                # Allow discharge of produced power, otherwise only charge
                # d.pwr_produced is negative, but self.produced is positive
                if setpoint > 0 and self.produced > SmartMode.POWER_START:
                    await self.power_discharge(min(self.produced, setpoint))
                else:
                    await self.power_charge(min(0, setpoint), time)

            case ManagerMode.MANUAL:
                # Manual power into or from home
                if (setpoint := int(self.manualpower.asNumber)) > 0:
                    await self.power_discharge(setpoint)
                else:
                    await self.power_charge(setpoint, time)

            case ManagerMode.OFF:
                self.operationstate.update_value(ManagerState.OFF.value)

    async def power_charge(self, setpoint: int, time: datetime) -> None:
        """Charge devices."""
        _LOGGER.info(f"Charge => setpoint {setpoint}W")

        # stop discharging devices
        for d in self.discharge:
            await d.power_discharge(0)

        # prevent hysteria
        if self.charge_time > time:
            if self.charge_time == datetime.max:
                self.charge_time = time + timedelta(seconds=2 if (time - self.charge_last).total_seconds() > 300 else 60)
                self.charge_last = self.charge_time
                self.pwr_low = 0
            setpoint = 0
        self.operationstate.update_value(ManagerState.CHARGE.value if setpoint < 0 else ManagerState.IDLE.value)

        # distribute charging devices
        dev_start = min(0, setpoint - self.charge_optimal * 2) if setpoint < -SmartMode.POWER_START else 0
        limit = self.charge_limit
        setpoint = max(limit, setpoint)
        for i, d in enumerate(sorted(self.charge, key=lambda d: d.electricLevel.asInt, reverse=True)):
            pwr = int(setpoint * (d.pwr_max * (100 - d.electricLevel.asInt)) / self.charge_weight)
            self.charge_weight -= d.pwr_max * (100 - d.electricLevel.asInt)

            # adjust the limit, make sure we have 'enough' power to charge
            limit -= d.pwr_max
            pwr = max(pwr, setpoint, d.pwr_max)
            if limit > setpoint - pwr:
                pwr = max(setpoint - limit, setpoint, d.pwr_max)

            # make sure we have devices in optimal working range
            if len(self.charge) > 1 and i == 0:
                self.pwr_low = 0 if (delta := d.charge_start * 1.5 - pwr) >= 0 else self.pwr_low + int(-delta)
                pwr = 0 if self.pwr_low < d.charge_optimal else pwr

            setpoint -= await d.power_charge(pwr)
            dev_start += -1 if pwr != 0 and d.electricLevel.asInt > self.idle_lvlmin + 3 else 0

        # start idle device if needed
        if dev_start < 0 and len(self.idle) > 0:
            self.idle.sort(key=lambda d: d.electricLevel.asInt, reverse=False)
            for d in self.idle:
                # offGrid device need to be started with at least their offgrid power, otherwise they will not be recognized as charging
                # but should not be started with more than pwr_offgrid if they are full
                # if a offGrid device need to be started, the output power is set to 0 and it take all offGrid power from grid
                await d.power_charge(-SmartMode.POWER_START - max(0, d.pwr_offgrid) if d.state != DeviceState.SOCFULL else -max(0, d.pwr_offgrid))
                if (dev_start := dev_start - d.charge_optimal * 2) >= 0:
                    break
            self.pwr_low: int = 0

    async def power_discharge(self, setpoint: int) -> None:
        """Discharge devices."""
        _LOGGER.info(f"Discharge => setpoint {setpoint}W")
        self.operationstate.update_value(ManagerState.DISCHARGE.value if setpoint > 0 else ManagerState.IDLE.value)

        # reset hysteria time
        if self.charge_time != datetime.max:
            self.charge_time = datetime.max
            self.pwr_low = 0

        # stop charging devices
        for d in self.charge:
            await d.power_discharge(0)

        # distribute discharging devices
        dev_start = max(0, setpoint - self.discharge_optimal * 2) if setpoint > SmartMode.POWER_START else 0
        solaronly = self.discharge_produced >= setpoint
        limit = self.discharge_produced if solaronly else self.discharge_limit
        setpoint = min(limit, setpoint)
        for i, d in enumerate(sorted(self.discharge, key=lambda d: d.electricLevel.asInt, reverse=False)):
            # calculate power to discharge
            if (pwr := int(setpoint * (d.pwr_max * d.electricLevel.asInt) / self.discharge_weight)) < -d.pwr_produced and d.state == DeviceState.SOCFULL:
                pwr = -d.pwr_produced
            self.discharge_weight -= d.pwr_max * d.electricLevel.asInt

            # adjust the limit, make sure we have 'enough' power to discharge
            limit -= -d.pwr_produced if solaronly else d.pwr_max
            if limit < setpoint - pwr:
                pwr = max(setpoint - limit, 0 if d.state != DeviceState.SOCFULL else -d.pwr_produced)
            pwr = min(pwr, setpoint, d.pwr_max)

            # make sure we have devices in optimal working range
            if len(self.discharge) > 1 and i == 0 and d.state != DeviceState.SOCFULL:
                self.pwr_low = 0 if (delta := d.discharge_start * 1.5 - pwr) <= 0 else self.pwr_low + int(delta)
                pwr = 0 if self.pwr_low > d.discharge_optimal else pwr

            setpoint -= await d.power_discharge(pwr)
            dev_start += 1 if pwr != 0 and d.electricLevel.asInt + 3 < self.idle_lvlmax else 0

        # start idle device if needed
        if dev_start > 0 and len(self.idle) > 0:
            self.idle.sort(key=lambda d: d.electricLevel.asInt, reverse=True)
            for d in self.idle:
                if d.state != DeviceState.SOCEMPTY:
                    await d.power_discharge(SmartMode.POWER_START)
                    if (dev_start := dev_start - d.discharge_optimal * 2) <= 0:
                        break
            self.pwr_low: int = 0
