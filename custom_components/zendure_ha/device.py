"""DistributionDevice: the generic contract every power device offers the distribution and the UI."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable

from homeassistant.core import HomeAssistant

from .const import Phase
from .entity import EntityDevice


DEADBAND_W = 20  # smallest power change worth a new command
FLOOR_PCT = 0.10  # device floor as a fraction of rated power
BAND_LOW_PCT = 0.30  # inverter band lower edge (the last/only active device may run below it)
BAND_HIGH_PCT = 0.95  # inverter band upper edge
REPORT_WAIT_S = 10.0  # a device awaiting the echo of its last command is left alone this long at most; one that stops reporting must not freeze the fleet
HOLD_S = 2.0  # over the broker there is no echo of the limits: a device is left alone this long after a command (measured 2026-09-22: the setpoint is in after 1.1 to 2.4 s, nothing dropped at 2 s)


class DeviceState(IntEnum):
    """Shared meaning for every vendor; the distribution relies on these semantics."""

    CREATED = 0  # just built, the device has never reported
    OFFLINE = 1  # unreachable: the vendor machinery is probing
    EXTERNAL = 2  # own behavior, not under our control
    SENT = 3  # command sent, awaiting the transport ack
    CONFIRM = 4  # acked, awaiting the output to follow
    ACTIVE = 5  # under control at a confirmed setpoint (0 W included)


@dataclass
class PowerProfile:
    """The rated power of one direction with the floor and band that follow from it; the subclass knows the direction."""

    power: int = 800

    def __post_init__(self):
        # bands are magnitudes, the floor keeps the direction of power (negative while charging)
        limit = abs(self.power)
        self.floor = int(limit * FLOOR_PCT) * (1 if self.power >= 0 else -1)
        self.band_low = int(limit * BAND_LOW_PCT)
        self.band_high = int(limit * BAND_HIGH_PCT)

    def available_power(self, device: DistributionDevice) -> int:
        return self.power

    def available_kwh(self, device: DistributionDevice) -> float:
        return 0.0


class ProfileDischarge(PowerProfile):
    """Discharge direction profile."""

    def available_power(self, device: DistributionDevice) -> int:
        if device.soc_limit == 2:  # the device's own verdict: empty
            return 0
        # thermal derating: at 55 °C or more, delivering 25 W or more under its setpoint for 25 s, the device is asked no more than it proves to manage plus 15 W
        if device.temp >= 55 and device.power_setpoint - device.power_actual > 25 and time.time() - device.power_since > 25:
            return min(self.power, device.power_setpoint + 15)
        return self.power

    def available_kwh(self, device: DistributionDevice) -> float:
        return device.kwh * device.soc / 100.0


class ProfileCharge(PowerProfile):
    """Charge direction profile."""

    def available_power(self, device: DistributionDevice) -> int:
        if device.soc_limit == 1:  # the device's own verdict: full
            return 0
        return self.power

    def available_kwh(self, device: DistributionDevice) -> float:
        return device.kwh * (device.soc_max - device.soc) / 100.0


class DistributionDevice(EntityDevice):
    """Generic contract only; the vendor implements the behavior and keeps every field and state truthful."""

    def __init__(self, hass: HomeAssistant, device_id: str, profiles: list[PowerProfile], name: str | None = None, subentry_id: str | None = None) -> None:
        super().__init__(hass, device_id, name, subentry_id)

        # what the device is doing; CREATED until the device has ever spoken
        self.state = DeviceState.CREATED
        self.state_since = 0.0
        self.on_invalidate: Callable | None = None
        self.soc = 0.0
        self.soc_min = 10.0
        self.soc_max = 90.0
        self.soc_limit = 0  # the device's own verdict: 1 = full, 2 = empty, 0 = neither
        self.solar = 0
        self.grid_off_power = 0  # W the off-grid socket draws; sits inside gridInputPower and is invisible to P1
        self.bypass = False  # panels straight to the house: that output is not ours to count or steer
        self.temp = 25.0
        self.kwh = 0.0
        self.wear = 0.0  # equivalent full cycles over the wear window, set by the distribution

        # power settings
        self.power_profile = profiles
        self.power_phase: Phase | None = None  # None: not known yet; restored from the device's own Store, not from the config
        self.power_setpoint = 0  # what the device is going to do under our control
        self.power_actual = 0  # measured charge/discharge value now; zeroed while OFFLINE
        self.power_since = 0.0  # last update of the power_setpoint

    async def update_phase(self, _entity: Any = None, _option: Any = None) -> None:
        # a changed phase regroups the fleet, so the order has to be rebuilt
        if self.on_invalidate is not None:
            self.on_invalidate()

    def power(self, watts: int) -> int:
        """Vendor contract: the device decides what it will do with the request; the return is the current power_actual."""
        return self.power_actual

    def power_off(self) -> None:
        """Vendor contract: end the control mode; the device returns to its own behavior."""

    def set_state(self, state: DeviceState) -> None:
        if state != self.state:
            if self.on_invalidate and (self.state <= DeviceState.EXTERNAL or state <= DeviceState.EXTERNAL):
                self.on_invalidate()
            self.state = state
            self.state_since = time.time()
