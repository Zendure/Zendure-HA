"""Coordinator for Zendure integration."""

from __future__ import annotations

import logging
import traceback
from collections import deque
from datetime import datetime, timedelta
from math import sqrt
from typing import Callable

from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import ManagerMode, ManagerState, SmartMode
from .device import ZendureDevice
from .sensor import ZendureSensor

_LOGGER = logging.getLogger(__name__)

CONST_POWER_NOACTION = 20
CONST_POWER_JUMP = 100
CONST_POWER_JUMP_HIGH = 250


class Distribution:
    """Manage power distribution for Zendure devices."""

    def __init__(self, hass: HomeAssistant, p1meter: str, setPoint: ZendureSensor) -> None:
        """Initialize Zendure Manager."""
        self.hass = hass
        self.setpoint_history: deque[int] = deque([0], maxlen=5)
        self.p1_avg = 0.0
        self.p1_factor = 1
        self.devices: list[ZendureDevice] = []
        self.setPoint = setPoint
        self.setpoint = 0
        self.operation: ManagerMode = ManagerMode.OFF
        self.manualpower = 0
        self._needs_update = datetime.min

        _LOGGER.debug("Updating P1 meter to: %s", p1meter)
        if p1meter:
            self.p1meterEvent = async_track_state_change_event(self.hass, [p1meter], self._p1_changed)
            if (entity := self.hass.states.get(p1meter)) is not None and entity.attributes.get("unit_of_measurement", "W") in ("kW", "kilowatt", "kilowatts"):
                self.p1_factor = 1000
        else:
            self.p1meterEvent = None

    @callback
    def _p1_changed(self, event: Event[EventStateChangedData]) -> None:
        # exit if there is nothing to do
        if not self.hass.is_running or not self.hass.is_running or (new_state := event.data["new_state"]) is None:
            return

        # convert the state to a integer value
        try:
            p1 = int(self.p1_factor * float(new_state.state))
        except ValueError:
            return

        # update the setpoint, and determine solar only mode
        setpoint, solar, totalweight, deviceWeight = self.init_distribute(p1)
        solarOnly = setpoint < 0 and solar >= abs(setpoint)
        setpoint += solar
        self.setPoint.update_value(setpoint)

        # calculate average and delta setpoint
        avg = int(sum(self.setpoint_history) / len(self.setpoint_history))
        if (delta := abs(avg - setpoint)) <= CONST_POWER_NOACTION and self._needs_update > datetime.now():
            return
        if delta > CONST_POWER_JUMP:
            self.setpoint_history.clear()
            if (setpoint * avg) < 0:
                setpoint = 0
        self.setpoint_history.append(setpoint)
        if delta > CONST_POWER_JUMP_HIGH:
            setpoint = int(0.75 * setpoint)

        match self.operation:
            case ManagerMode.MATCHING_DISCHARGE:
                setpoint = min(setpoint, 0)
            case ManagerMode.MATCHING_CHARGE:
                setpoint = max(setpoint, 0)
            case ManagerMode.MANUAL:
                setpoint = self.setPoint.asInt
            case ManagerMode.OFF:
                return

        # distribute power
        for d in self.devices:
            if solarOnly:
                setpoint -= d.distribute(max(setpoint, -d.solarPower.asInt))
            else:
                weight = deviceWeight(d)
                power = int(setpoint * weight / totalweight if totalweight != 0 else setpoint if weight != 0 else 0)
                power = max(power, setpoint) if setpoint < 0 else min(power, setpoint)
                setpoint -= d.distribute(power)
                totalweight -= weight
        self._needs_update = datetime.now() + timedelta(seconds=30)

    def init_distribute(self, setpoint: int) -> tuple[int, int, float, Callable[[ZendureDevice], float]]:
        # update the power
        solar = 0
        chargelimit = 0
        dischargelimit = 0
        chargeWeight = 0
        dischargeWeight = 0

        for d in self.devices:
            if (home := d.homePower.asInt) != 0:
                chargelimit += d.charge_limit
                dischargelimit += d.discharge_limit
                chargeWeight += self.weightcharge(d)
                dischargeWeight += self.weightdischarge(d)
            d.power_offset = d.solarPower.asInt
            solar += d.power_offset
            if d.offGrid is not None:
                if (off_grid := d.offGrid.asInt) < 0:
                    home += off_grid
                else:
                    solar += off_grid
                d.power_offset += min(0, off_grid)
            setpoint += home

        return (setpoint, solar, chargeWeight, self.weightcharge) if setpoint < 0 else (setpoint, solar, dischargeWeight, self.weightdischarge)

    def weightcharge(self, d: ZendureDevice) -> float:
        return d.charge_limit * (d.kWh - d.availableKwh.asNumber) if d.electricLevel.asInt < d.socSet.asNumber else 0.0

    def weightdischarge(self, d: ZendureDevice) -> float:
        return d.discharge_limit * d.availableKwh.asNumber if d.electricLevel.asInt > d.minSoc.asNumber else 0.0
