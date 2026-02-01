"""Coordinator for Zendure integration."""

from __future__ import annotations

import logging
import traceback
from collections import deque
from datetime import datetime
from typing import Callable

from homeassistant.components import persistent_notification
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

    def __init__(self, hass: HomeAssistant, p1meter: str, setpoint_sensor: ZendureSensor) -> None:
        """Initialize Zendure Manager."""
        self.hass = hass
        self.weights: list[Callable[[ZendureDevice], float]] = [self.weightcharge, self.weightdischarge]
        self.sorts: list[Callable[[ZendureDevice], float]] = [self.sortcharge, self.sortdischarge]
        self.Max: list[Callable[[int, int], int]] = [min, max]
        self.Min: list[Callable[[int, int], int]] = [max, min]
        self.start: list[int] = [-CONST_POWER_START, CONST_POWER_START]
        self.setpoint_history: deque[int] = deque([0], maxlen=5)
        self.p1_avg = 0.0
        self.p1_factor = 1
        self.devices: list[ZendureDevice] = []
        self.setpoint_sensor = setpoint_sensor
        self.setpoint = 0
        self.operation: ManagerMode = ManagerMode.OFF
        self.manualpower = 0

        _LOGGER.debug("Updating P1 meter to: %s", p1meter)
        if p1meter:
            self.p1meterEvent = async_track_state_change_event(self.hass, [p1meter], self._p1_changed)
            if (entity := self.hass.states.get(p1meter)) is not None and entity.attributes.get("unit_of_measurement", "W") in ("kW", "kilowatt", "kilowatts"):
                self.p1_factor = 1000
        else:
            self.p1meterEvent = None

    def set_operation(self, operation: ManagerMode) -> None:
        """Set the operation mode."""
        self.operation = operation
        if self.p1meterEvent is not None:
            if operation != ManagerMode.OFF and (len(self.devices) == 0 or all(d.status == DeviceState.ACTIVE for d in self.devices)):
                _LOGGER.warning("No devices online, not possible to start the operation")
                persistent_notification.async_create(self.hass, "No devices online, not possible to start the operation", "Zendure", "zendure_ha")
                return

            match self.operation:
                case ManagerMode.OFF:
                    if len(self.devices) > 0:
                        for d in self.devices:
                            d.power_off()

    @callback
    def _p1_changed(self, event: Event[EventStateChangedData]) -> None:
        # exit if there is nothing to do
        if not self.hass.is_running or not self.hass.is_running or (new_state := event.data["new_state"]) is None:
            return

        # convert the state to a integer value
        try:
            p1 = int(self.p1_factor * float(new_state.state))
            self.update(p1, datetime.now())
        except ValueError:
            return

    def update(self, p1: int, time: datetime) -> None:
        try:
            # update the setpoint, and determine solar only mode
            setpoint, solar = self.get_setpoint(p1)
            solarOnly = setpoint > 0 and solar > setpoint
            self.setpoint_sensor.update_value(setpoint)

            # calculate average and delta setpoint
            avg = int(sum(self.setpoint_history) / len(self.setpoint_history))
            if (abs(delta := avg - setpoint)) > CONST_POWER_JUMP:
                self.setpoint_history.clear()
                if delta > CONST_POWER_JUMP_HIGH:
                    setpoint = int(avg - 0.75 * delta)

            if (setpoint * avg) < 0:
                setpoint = 0
            self.setpoint_history.append(setpoint)

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
                for d in sorted(self.devices, key=self.sortdischarge, reverse=False):
                    setpoint -= d.distribute(min(setpoint, d.solarPower.asInt), time)
            else:
                idx = 0 if setpoint < 0 else 1
                self.distrbute(setpoint, idx, self.weights[idx], time)

        except Exception as err:
            _LOGGER.error(f"Error mqtt_message_received {err}!")
            _LOGGER.error(traceback.format_exc())

    def get_setpoint(self, setpoint: int) -> tuple[int, int]:
        # update the power
        solar = 0
        for d in self.devices:
            if d.status != DeviceState.ACTIVE or d.fuseGrp is None:
                continue
            setpoint += d.homePower.asInt
            solar += d.solarPower.asInt
            d.fuseGrp.initPower = True
            if d.offGrid is not None:
                if (off_grid := d.offGrid.asInt) < 0:
                    solar += -off_grid
                else:
                    setpoint -= off_grid
                d.power_offset = max(0, off_grid)

        return (setpoint, solar)

    def distrbute(self, setpoint: int, idx: int, deviceWeight: Callable[[ZendureDevice], float], time: datetime) -> None:
        """Distribute power to devices."""
        used_devices: list[ZendureDevice] = []
        totalpower = 0
        totalweight = 0.0
        start = setpoint
        for d in sorted(self.devices, key=self.sorts[idx], reverse=idx == 1):
            if d.status != DeviceState.ACTIVE or d.fuseGrp is None:
                continue
            if (weight := deviceWeight(d)) == 0.0:
                d.distribute(0, time)
            elif d.homePower.asInt == 0:
                # Check if we must start this device
                if startdevice := weight > 0 and start != 0:
                    start = self.Max[idx](0, int(start - d.limit[idx] * CONST_HIGH))
                d.distribute(self.start[idx] if startdevice else 0, time)
            elif len(used_devices) == 0 or setpoint / (totalpower + d.limit[idx]) >= CONST_LOW:
                # update the device power
                used_devices.append(d)
                d.power_limit = d.fuseGrp.devicelimit(d, idx)
                totalpower += d.power_limit
                totalweight += weight
                start = self.Max[idx](0, int(start - d.limit[idx] * CONST_HIGH))
            else:
                # Stop the device
                d.distribute(0, time)

        if totalpower == 0 or totalweight == 0.0:
            return

        fixedpct = min(CONST_FIXED, abs(setpoint / totalpower) if totalpower != 0 else 0.0)
        for d in used_devices:
            # calculate the device home power, make sure we have 'enough' power for the setpoint
            flexible = 0 if fixedpct < CONST_FIXED else setpoint - CONST_FIXED * totalpower
            totalpower -= d.power_limit
            weight = deviceWeight(d)
            power = 0 if totalweight == 0 else int(fixedpct * d.limit[idx] + flexible * (weight / totalweight)) if totalpower != 0 else setpoint
            power = self.Min[idx](d.limit[idx], self.Max[idx](power, setpoint - totalpower))
            setpoint -= d.distribute(power, time)

            # adjust the totals
            totalweight = round(totalweight - weight, 2)

    @staticmethod
    def weightcharge(d: ZendureDevice) -> float:
        return (d.kWh - d.availableKwh.asNumber) if d.level < 100 else 0.0

    @staticmethod
    def weightdischarge(d: ZendureDevice) -> float:
        return d.availableKwh.asNumber if d.level > 0 else 0.0

    @staticmethod
    def sortcharge(d: ZendureDevice) -> float:
        return d.level - (0 if d.homePower.asInt == 0 else 3)

    @staticmethod
    def sortdischarge(d: ZendureDevice) -> float:
        return d.level + (0 if d.homePower.asInt == 0 else 3)
