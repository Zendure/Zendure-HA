"""Coordinator for Zendure integration."""

from __future__ import annotations

import logging
import traceback
from collections import deque
from typing import Callable

from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import ManagerMode
from .device import DeviceState, ZendureDevice
from .sensor import ZendureSensor

_LOGGER = logging.getLogger(__name__)

CONST_POWER_START = 50
CONST_POWER_JUMP = 100
CONST_POWER_JUMP_HIGH = 250
CONST_FIXED = 0.1
CONST_HIGH = 0.55
CONST_LOW = 0.15


class Distribution:
    """Manage power distribution for Zendure devices."""

    def __init__(self, hass: HomeAssistant, p1meter: str, setPoint: ZendureSensor) -> None:
        """Initialize Zendure Manager."""
        self.hass = hass
        self.weights: list[Callable[[ZendureDevice], float]] = [self.weightcharge, self.weightdischarge]
        self.Max: list[Callable[[int, int], int]] = [min, max]
        self.Min: list[Callable[[int, int], int]] = [max, min]
        self.start: list[int] = [-CONST_POWER_START, CONST_POWER_START]
        self.setpoint_history: deque[int] = deque([0], maxlen=5)
        self.p1_avg = 0.0
        self.p1_factor = 1
        self.devices: list[ZendureDevice] = []
        self.setPoint = setPoint
        self.setpoint = 0
        self.operation: ManagerMode = ManagerMode.OFF
        self.manualpower = 0
        self._low_pwr = 0.0

        _LOGGER.debug("Updating P1 meter to: %s", p1meter)
        if p1meter:
            self.p1meterEvent = async_track_state_change_event(self.hass, [p1meter], self._p1_changed)
            if (entity := self.hass.states.get(p1meter)) is not None and entity.attributes.get("unit_of_measurement", "W") in ("kW", "kilowatt", "kilowatts"):
                self.p1_factor = 1000
        else:
            self.p1meterEvent = None

    @callback
    def _p1_changed(self, event: Event[EventStateChangedData]) -> None:
        try:
            # exit if there is nothing to do
            if not self.hass.is_running or not self.hass.is_running or (new_state := event.data["new_state"]) is None:
                return

            # convert the state to a integer value
            try:
                p1 = int(self.p1_factor * float(new_state.state))
            except ValueError:
                return

            # update the setpoint, and determine solar only mode
            setpoint, solar = self.get_setpoint(p1)
            solarOnly = setpoint < 0 and solar >= abs(setpoint)
            setpoint += solar
            self.setPoint.update_value(setpoint)

            # calculate average and delta setpoint
            avg = int(sum(self.setpoint_history) / len(self.setpoint_history))
            if (delta := abs(avg - setpoint)) > CONST_POWER_JUMP:
                self.setpoint_history.clear()
                if (setpoint * avg) < 0:
                    setpoint = 0
            self.setpoint_history.append(setpoint)
            setpoint = int(0.75 * setpoint) if delta > CONST_POWER_JUMP_HIGH else (setpoint + 2 * avg) // 3

            match self.operation:
                case ManagerMode.MATCHING_DISCHARGE:
                    setpoint = max(setpoint, 0)
                case ManagerMode.MATCHING_CHARGE:
                    setpoint = min(setpoint, 0)
                case ManagerMode.MANUAL:
                    setpoint = self.manualpower
                case ManagerMode.OFF:
                    return

            # distribute power
            if solarOnly:
                for d in self.devices:
                    setpoint -= d.distribute(max(setpoint, -d.solarPower.asInt))
            else:
                idx = 0 if setpoint < 0 else 1
                self.distrbute(setpoint, idx, self.weights[idx])

        except Exception as err:
            _LOGGER.error(f"Error mqtt_message_received {err}!")
            _LOGGER.error(traceback.format_exc())

    def get_setpoint(self, setpoint: int) -> tuple[int, int]:
        # update the power
        solar = 0
        for d in self.devices:
            if d.status != DeviceState.ACTIVE:
                continue
            home = d.homePower.asInt
            d.power_offset = d.solarPower.asInt
            solar += d.power_offset
            if d.offGrid is not None:
                if (off_grid := d.offGrid.asInt) < 0:
                    home += off_grid
                else:
                    solar += off_grid
                d.power_offset += min(0, off_grid)
            setpoint += home

        return (setpoint, solar)

    def distrbute(self, setpoint: int, idx: int, deviceWeight: Callable[[ZendureDevice], float]) -> None:
        """Distribute power to devices based on weights."""
        used_devices: list[ZendureDevice] = []
        totalpower = 0
        totalweight = 0.0
        start = setpoint
        for d in sorted(self.devices, key=lambda d: d.level // 3, reverse=idx == 1):
            if d.status != DeviceState.ACTIVE:
                continue
            weight = deviceWeight(d)
            if d.homePower.asInt == 0:
                # Check if we must start this device
                if startdevice := weight > 0 and start != 0:
                    start = self.Max[idx](0, int(start - d.limit[idx] * CONST_HIGH))
                d.distribute(self.start[idx] if startdevice else 0)
            elif len(used_devices) == 0 or setpoint / (totalpower + d.limit[idx]) >= CONST_LOW:
                # update the device power
                used_devices.append(d)
                totalpower += d.limit[idx]
                totalweight += weight
                start = self.Max[idx](0, int(start - d.limit[idx] * CONST_HIGH))
            else:
                # Stop the device
                d.distribute(0)

        if totalpower == 0 or totalweight == 0.0:
            return

        fixedpct = min(CONST_FIXED, abs(setpoint / totalpower) if totalpower != 0 else 0.0)
        for d in used_devices:
            # calculate the device home power, make sure we have 'enough' power for the setpoint
            flexible = 0 if fixedpct < CONST_FIXED else setpoint - CONST_FIXED * totalpower
            totalpower -= d.limit[idx]
            weight = deviceWeight(d)
            power = int(fixedpct * d.limit[idx] + flexible * (weight / totalweight)) if totalpower != 0 else setpoint
            power = self.Min[idx](d.limit[idx], self.Max[idx](power, setpoint - totalpower))
            setpoint -= d.distribute(power)

            # adjust the totals
            totalweight = round(totalweight - weight, 2)

    @staticmethod
    def weightcharge(d: ZendureDevice) -> float:
        return (d.kWh - d.availableKwh.asNumber) if d.electricLevel.asInt < d.socSet.asNumber and d.socLimit.asInt != 1 else 0.0

    @staticmethod
    def weightdischarge(d: ZendureDevice) -> float:
        return d.availableKwh.asNumber if d.electricLevel.asInt > d.minSoc.asNumber and d.socLimit.asInt != 2 else 0.0
