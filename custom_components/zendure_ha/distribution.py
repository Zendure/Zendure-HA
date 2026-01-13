"""Coordinator for Zendure integration."""

from __future__ import annotations

import logging
import traceback
from collections import deque
from datetime import datetime, timedelta
from typing import Callable

from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from numpy import sort

from .const import ManagerMode
from .device import DeviceState, ZendureDevice
from .sensor import ZendureSensor

_LOGGER = logging.getLogger(__name__)

CONST_POWER_NOACTION = 12
CONST_POWER_START = 50
CONST_POWER_JUMP = 100
CONST_POWER_JUMP_HIGH = 250
CONST_FIXED = 0.1


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
        self._percentage = 0.0
        self._needs_update = 0.0

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
            setpoint, solar, totalpower, totalweight, deviceWeight = self.init_distribute(p1)
            solarOnly = setpoint < 0 and solar >= abs(setpoint)
            setpoint += solar
            self.setPoint.update_value(setpoint)

            # calculate average and delta setpoint
            avg = int(sum(self.setpoint_history) / len(self.setpoint_history))
            if (delta := abs(avg - setpoint)) <= CONST_POWER_NOACTION and self._needs_update == 0.0:
                _LOGGER.debug("No significant change in power distribution (delta=%d, avg=%d, setpoint=%d)", delta, avg, setpoint)
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
            _LOGGER.info("Distributing power setpoint %dW (solarOnly=%s)", setpoint, solarOnly)
            if solarOnly:
                for d in self.devices:
                    setpoint -= d.distribute(max(setpoint, -d.solarPower.asInt))
            else:
                self.distrbute(setpoint, totalpower, totalweight, deviceWeight)

        except Exception as err:
            _LOGGER.error(f"Error mqtt_message_received {err}!")
            _LOGGER.error(traceback.format_exc())

    def init_distribute(self, setpoint: int) -> tuple[int, int, int, float, Callable[[ZendureDevice], float]]:
        # update the power
        solar = 0
        limit = [0, 0]
        weight = [0.0, 0.0]
        available = 0

        time = datetime.now()
        for d in self.devices:
            if d.status != DeviceState.ACTIVE:
                continue

            if (home := d.homePower.asInt if time > d.power_time else d.power_setpoint) != 0:
                limit[0] += d.limit[0]
                limit[1] += d.limit[1]
                weight[0] += self.weightcharge(d)
                weight[1] += self.weightdischarge(d)
            elif d.level > 0:
                available += 1

            d.power_offset = d.solarPower.asInt
            solar += d.power_offset
            if d.offGrid is not None:
                if (off_grid := d.offGrid.asInt) < 0:
                    home += off_grid
                else:
                    solar += off_grid
                d.power_offset += min(0, off_grid)
            setpoint += home

        idx = 0 if setpoint < 0 else 1
        return (setpoint, solar, limit[idx], weight[idx], self.weights[idx])

    def distrbute(self, setpoint: int, totalpower: int, totalweight: float, deviceWeight: Callable[[ZendureDevice], float]) -> None:
        """Distribute power to devices based on weights."""
        self._percentage = abs(setpoint / totalpower) if totalpower != 0 else 0.0
        self._needs_update = 0.0 if self._percentage > 2 * CONST_FIXED else self._needs_update + CONST_FIXED * 2 - self._percentage
        fixedpct = min(CONST_FIXED, self._percentage)
        flexible = 0 if fixedpct < CONST_FIXED else setpoint - CONST_FIXED * totalpower
        idx = 0 if setpoint < 0 else 1
        for d in sorted(self.devices, key=lambda d: d.level / 4, reverse=idx == 1):
            if d.status != DeviceState.ACTIVE:
                continue

            # Set inactive devices to zero power
            if d.homePower.asInt == 0:
                if start := totalpower < setpoint and deviceWeight(d) > 0:
                    totalpower -= d.limit[idx]
                d.distribute(self.start[idx] if start else 0)
                continue

            # calculate the device home power, make sure we have 'enough' power for the setpoint
            totalpower -= d.limit[idx]
            weight = deviceWeight(d)
            power = int(fixedpct * d.limit[idx] + flexible * (weight / totalweight)) if totalweight != 0 else setpoint if weight != 0 else 0
            power = self.Min[idx](d.limit[idx], self.Max[idx](power, setpoint - totalpower))
            setpoint -= d.distribute(power)

            # adjust the totals
            totalweight = round(totalweight - weight, 2)
            flexible -= power - fixedpct * d.limit[idx]
        _LOGGER.debug("Distribution complete, remaining setpoint: %dW", setpoint)

    @staticmethod
    def weightcharge(d: ZendureDevice) -> float:
        return d.limit[0] * (d.kWh - d.availableKwh.asNumber) if d.electricLevel.asInt < d.socSet.asNumber else 0.0

    @staticmethod
    def weightdischarge(d: ZendureDevice) -> float:
        return d.limit[1] * d.availableKwh.asNumber if d.electricLevel.asInt > d.minSoc.asNumber else 0.0
