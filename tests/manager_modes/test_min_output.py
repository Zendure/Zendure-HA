"""Minimum discharge output (min_output_w) — cross-mode conformance + lifecycle.

The minimum-discharge floor keeps the microinverter engaged above its idle
threshold so it responds instantly instead of going through the
grid-reconnect/soft-restart delay. Scoped to the HUB/AIO device family.

Two behaviours live here:

1. Steady-state conformance (CSV-driven, ``data/min_output.csv``): the floor
   holds discharge commands at min_output in discharge modes, never engages
   the battery in battery-preserving modes, and changes nothing when demand
   is already above the floor.

2. Command-assertion lifecycle tests: ``awake`` follows the manager's power
   direction, so a discharge command right after boot (cold start) engages
   the floor, and the stop-discharge calls inside a charge period never do.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.zendure_ha.const import DeviceState, ManagerMode, PowerFlowDirection
from custom_components.zendure_ha.manager import ZendureManager

from .harness import Case, assert_matches_spec, drive_metered, load_cases_from_csv, make_params, run_step

CASES = load_cases_from_csv("min_output")


@pytest.mark.parametrize("case", make_params(CASES))
async def test_min_output_matches_spec(case: Case) -> None:
    devs = await drive_metered(ManagerMode[case.mode], case)

    assert_matches_spec(devs, case)


# --- lifecycle (command-assertion style, as in test_soc_boundaries.py) ---


def _sensor(value: float = 0) -> SimpleNamespace:
    return SimpleNamespace(asInt=int(value), asNumber=float(value))


class _FakeFuseGroup:
    """Stand-in for the device's fuse group; returns the device's own limits."""

    def __init__(self) -> None:
        self.maxpower = 3600
        self.minpower = -3600
        self.initPower = True

    def charge_limit(self, d: "_FakeDevice") -> int:
        return max(self.minpower, d.charge_limit)

    def discharge_limit(self, d: "_FakeDevice") -> int:
        return min(self.maxpower, d.discharge_limit)


class _FakeDevice:
    """Minimal device exposing what powerChanged/power_discharge/power_charge touch."""

    def __init__(self, *, electric_level: int, state: DeviceState, min_output: int = 0) -> None:
        self.name = "dev"
        self.state = state
        self.electricLevel = _sensor(electric_level)
        self.homeOutput = _sensor(0)
        self.homeInput = _sensor(0)
        self.batteryInput = _sensor(0)
        self.batteryOutput = _sensor(0)
        self.solarInput = _sensor(0)
        self.byPass = _sensor(0)
        self.minSoc = _sensor(0)
        self.socLimit = _sensor(0)
        self.pwr_max = 1200
        self.pwr_offgrid = 0
        self.pwr_produced = 0
        self.pwr_bypass = 0
        self.exports_bypass = True
        self.kWh = 10.0
        self.actualKwh = 10.0
        self.min_output = min_output
        self.awake = False
        self._floor_battery_preserving = False
        self.charge_optimal = 300
        self.charge_start = 120
        self.discharge_optimal = 300
        self.discharge_start = 120
        self.charge_limit = -1200
        self.discharge_limit = 1200
        self.fuseGrp = _FakeFuseGroup()
        self.discharge_calls: list[int] = []
        self.charge_calls: list[int] = []

    @property
    def online(self) -> bool:
        return True

    def on_direction_change(self, direction: PowerFlowDirection, *, battery_preserving: bool = False) -> None:
        self.awake = direction == PowerFlowDirection.DISCHARGING
        self._floor_battery_preserving = battery_preserving

    async def power_get(self) -> bool:
        return self.state != DeviceState.OFFLINE

    async def power_discharge(self, power: int) -> int:
        self.discharge_calls.append(power)
        return power

    async def power_charge(self, power: int) -> int:
        self.charge_calls.append(power)
        return power


def _manager(device: _FakeDevice) -> ZendureManager:
    mgr = object.__new__(ZendureManager)
    mgr.operation = ManagerMode.MATCHING
    mgr.devices = [device]
    mgr.simulation = False
    mgr.charge_time = datetime.max
    mgr.charge_last = datetime.min
    mgr.pwr_low = 0
    mgr.operationstate = SimpleNamespace(value=None, update_value=lambda v: None)
    mgr.power = SimpleNamespace(value=None, update_value=lambda v: None)
    mgr.availableKwh = SimpleNamespace(value=None, update_value=lambda v: None)
    mgr.globalSoc = SimpleNamespace(value=None, update_value=lambda v: None)
    mgr.charge = []
    mgr.charge_limit = 0
    mgr.charge_optimal = 0
    mgr.charge_weight = 0
    mgr.discharge = []
    mgr.discharge_bypass = 0
    mgr.discharge_limit = 0
    mgr.discharge_optimal = 0
    mgr.discharge_produced = 0
    mgr.discharge_weight = 0
    mgr.idle = []
    mgr.idle_lvlmax = 0
    mgr.idle_lvlmin = 100
    mgr.produced = 0
    return mgr


async def test_cold_start_discharge_engages_floor() -> None:
    """Cold start: the first powerChanged in discharge direction applies min_output.

    awake starts False on a fresh device (like a fresh HA restart) and no
    charge period happened yet. The idle-start kickstart commands 50 W; the
    floor must raise that first command to min_output.
    """
    device = _FakeDevice(electric_level=50, state=DeviceState.INACTIVE, min_output=100)
    mgr = _manager(device)

    await mgr.powerChanged(60, False, datetime.now())

    assert device.discharge_calls == [100]


async def test_charge_period_stop_discharge_does_not_engage_floor() -> None:
    """A stop-discharge call inside a charge period must not apply the floor."""

    device = _FakeDevice(electric_level=50, state=DeviceState.INACTIVE, min_output=100)
    mgr = _manager(device)
    mgr.discharge = [device]  # the device was discharging; the charge period stops it

    await mgr.power_charge(-300, datetime.now())

    assert device.discharge_calls == [0]
    assert device.awake is False  # the charge direction persists


async def test_charge_direction_idle_device_stays_out_of_discharge() -> None:
    """An idle min_output device during a charge period must not be classified
    as discharging: the classification branch is gated on awake, which follows
    the manager's power direction."""

    device = _FakeDevice(electric_level=50, state=DeviceState.INACTIVE, min_output=100)
    mgr = _manager(device)
    device.awake = False  # the manager is in charge direction

    await mgr.powerChanged(-300, False, datetime.now())

    assert device not in mgr.discharge
    assert device.discharge_calls == []
    assert device.awake is False  # the charge direction persists


async def test_matching_charge_discharge_call_caps_floor_at_own_solar() -> None:
    """MATCHING_CHARGE may only pass solar through - it must never discharge the
    battery. The floor is not disabled outright: it still applies, but capped at
    the device's own solar, so it can never push the command past what solar
    alone covers. Here solar (60W) is below the floor (100W), so the command
    stays at the solar-capped level, not the full 100W floor."""

    device = _FakeDevice(electric_level=50, state=DeviceState.INACTIVE, min_output=100)
    mgr = _manager(device)
    mgr.operation = ManagerMode.MATCHING_CHARGE
    device.awake = True  # a previous discharge period engaged the direction
    mgr.charge_time = datetime.now() - timedelta(minutes=1)  # charge period released
    device.solarInput = _sensor(60)
    device.homeInput = _sensor(-60)  # 60 W solar currently flowing to home

    await mgr.powerChanged(300, False, datetime.now())

    # pass-through command is capped at solar (60W); the floor (100W) exceeds
    # what solar can cover, so it's never fully reached - never touches the battery
    assert device.discharge_calls == [60]
    assert device.awake is True
    assert device._floor_battery_preserving is True


async def test_matching_charge_discharge_call_reaches_floor_when_solar_covers_it() -> None:
    """Same MATCHING_CHARGE solar-capped floor, but with own solar (200W) comfortably
    above the floor (100W) while the naturally-computed demand-driven share (20W,
    from a low P1) sits below it: the command should be raised to the full
    min_output, proving the cap is a ceiling that still lets the floor raise a
    low natural share, not a permanent disable."""

    device = _FakeDevice(electric_level=50, state=DeviceState.INACTIVE, min_output=100)
    mgr = _manager(device)
    mgr.operation = ManagerMode.MATCHING_CHARGE
    device.awake = True
    mgr.charge_time = datetime.now() - timedelta(minutes=1)
    device.solarInput = _sensor(200)
    device.homeInput = _sensor(-200)  # 200W of own solar available

    # setpoint = p1 + home = -180 + 200 = 20W: well below both solar and the floor
    await mgr.powerChanged(-180, False, datetime.now())

    assert device.discharge_calls == [100]


async def test_empty_battery_floor_is_skipped() -> None:
    """SOCEMPTY can't discharge the battery: the floor must not apply, and the
    idle-start kickstart skips empty batteries entirely."""

    device = _FakeDevice(electric_level=5, state=DeviceState.SOCEMPTY, min_output=100)
    mgr = _manager(device)

    await mgr.powerChanged(200, False, datetime.now())

    assert device.discharge_calls == []


async def test_floored_peer_does_not_cause_over_allocation() -> None:
    """A device the distributor parks at pwr=0 can still be floored back up to
    min_output. The manager must credit that ACTUAL output against setpoint,
    not the pre-floor pwr it originally computed - otherwise the peer device
    gets the full setpoint on top of the floor, and combined output overshoots.
    """
    dev1 = _FakeDevice(electric_level=10, state=DeviceState.INACTIVE, min_output=100)
    dev1.awake = True
    dev1.pwr_max = 200
    dev1.discharge_start = 20  # threshold (*1.5=30) above the ~10W share it will compute
    dev1.discharge_optimal = 50

    dev2 = _FakeDevice(electric_level=90, state=DeviceState.INACTIVE, min_output=0)
    dev2.awake = True
    dev2.pwr_max = 200

    mgr = _manager(dev1)
    mgr.discharge = [dev1, dev2]
    mgr.discharge_limit = dev1.pwr_max + dev2.pwr_max
    mgr.discharge_optimal = 100
    mgr.discharge_weight = dev1.pwr_max * dev1.electricLevel.asInt + dev2.pwr_max * dev2.electricLevel.asInt
    mgr.pwr_low = 500  # already elevated from prior cycles: dev1 is a parking candidate

    await mgr.power_discharge(100)

    assert dev1.discharge_calls == [100]  # parked to 0, then floored back up
    assert dev2.discharge_calls == [0]  # must not also get the setpoint dev1 already covers
    assert sum(dev1.discharge_calls) + sum(dev2.discharge_calls) == 100  # matches setpoint


async def test_floored_solo_device_does_not_trigger_unnecessary_idle_start() -> None:
    """A lone discharging device's own min_output floor can push its actual
    output past setpoint on its own. The 'protect the low-SoC device' idle-start
    heuristic must not then wake an idle peer too - the floor isn't relieved by
    adding another device, so starting one only adds unwanted export. Without
    this, the two devices oscillate cycle-to-cycle instead of settling.
    """
    dev1 = _FakeDevice(electric_level=10, state=DeviceState.INACTIVE, min_output=100)
    dev1.awake = True

    dev2 = _FakeDevice(electric_level=90, state=DeviceState.INACTIVE, min_output=0)

    mgr = _manager(dev1)
    mgr.discharge = [dev1]
    mgr.idle = [dev2]
    mgr.idle_lvlmax = 90
    mgr.discharge_limit = dev1.pwr_max
    mgr.discharge_optimal = dev1.discharge_optimal
    mgr.discharge_weight = dev1.pwr_max * dev1.electricLevel.asInt

    await mgr.power_discharge(50)

    assert dev1.discharge_calls == [100]  # floored well past the 50W setpoint on its own
    assert dev2.discharge_calls == []  # must not be woken; the overshoot isn't relieved by it


async def test_idle_floor_devices_own_solar_counts_toward_dispatch_produced() -> None:
    """A device that's idle but floor-eligible (home==0, min_output>0, awake) still
    has its own solar. The per-device solar totals already counted it, but
    discharge_produced didn't - it only added up devices that were actively
    exporting. That made the safety clamp think the group had less solar than
    it really did, which broke the SoC-weighted split: the actively-exporting
    peer kept overshooting its share instead of shrinking, and this device's
    own solar never reached home. Taken from a real log where one device got
    stuck at 668W (target 197W) and the other stuck at 0W.
    """
    dev1 = _FakeDevice(electric_level=13, state=DeviceState.INACTIVE, min_output=90)
    dev1.awake = True
    dev1.homeOutput = _sensor(668)  # actively exporting: home>0 branch

    dev2 = _FakeDevice(electric_level=11, state=DeviceState.INACTIVE, min_output=102)
    dev2.awake = True
    dev2.batteryInput = _sensor(566)  # idle-with-floor branch: home==0, but real solar

    mgr = _manager(dev1)
    mgr.devices = [dev1, dev2]

    await mgr.powerChanged(-307, False, datetime.now())  # setpoint = -307 + 668 + 0 = 361

    # SoC-weighted (13:11) split of the 361W setpoint: dev1 shrinks from 668W
    # down to its fair ~195W share, dev2 rises from 0W to its ~166W share.
    # Neither needs its battery: both shares stay well under each device's own
    # solar (668W / 566W).
    # The command ramp caps one cycle at 50W from the device's real output
    # (668W / 0W), so the first commands land on the way to the shares, not on
    # them: 668-50=618W down, 0+50 then floored to 102W up.
    assert dev1.discharge_calls == [618]
    assert dev2.discharge_calls == [102]


async def test_steep_setpoint_change_ramps_command_in_steps() -> None:
    """A steep demand change must reach the hub as 50W steps, not one jump.

    Measured 2026-09-22: a ~100W+ change of the automation value between two
    5s cycles made the hub confirm the command and then charge its battery
    instead of delivering it, for minutes. The ramp caps the per-cycle change
    against the device's real output; the floor still holds every step.
    Without the ramp the sequence would be [580, 400, 220, 100] - a 180W step.
    """
    device = _FakeDevice(electric_level=67, state=DeviceState.INACTIVE, min_output=100)
    device.homeOutput = _sensor(280)  # the hub delivers 280W when demand collapses
    mgr = _manager(device)

    await run_step(mgr, 300)  # demand spike: command steps up to 330
    for _ in range(4):
        device.homeOutput = _sensor(device.discharge_calls[-1])  # ideal device: delivers the command
        await run_step(mgr, -180)

    assert device.discharge_calls == [330, 280, 230, 180, 130]
    assert all(call >= 100 for call in device.discharge_calls)


async def test_big_demand_jump_bypasses_ramp() -> None:
    """A demand jump over 500W sends the command immediately, no 50W steps.

    A kettle switching on must not wait a minute of 50W steps while the grid
    covers the gap. Without the skip the first command would be 330W
    (a 50W step from the 280W the device delivers).
    """
    device = _FakeDevice(electric_level=67, state=DeviceState.INACTIVE, min_output=100)
    device.homeOutput = _sensor(280)  # the hub delivers 280W before the jump
    mgr = _manager(device)

    await run_step(mgr, 800)  # kettle: setpoint = 800 + 280 = 1080W

    assert device.discharge_calls == [1080]  # the full target in one step


async def test_manual_discharge_never_engages_floor() -> None:
    """MANUAL is user-driven: the command should be exactly what was asked for,
    never silently raised by the min_output floor. Mirrors
    test_cold_start_discharge_engages_floor's kickstart setup (60W, so the
    idle-start kickstart fires and commands POWER_START=50W) but in MANUAL
    mode, where that 50W must NOT be floored up to the 100W min_output."""

    device = _FakeDevice(electric_level=50, state=DeviceState.INACTIVE, min_output=100)
    mgr = _manager(device)
    mgr.operation = ManagerMode.MANUAL
    mgr.manualpower = _sensor(60)

    await mgr.powerChanged(0, False, datetime.now())

    assert device.discharge_calls == [50]
    assert device.awake is False
