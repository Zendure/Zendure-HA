"""Scenario tests for the P1 bias applied in ZendureManager.powerChanged.

The bias keeps `bias` W drawn from the grid while discharging, so that battery
power is never fed into the grid, but it must never hold back solar and never
cause charging from the grid. Like test_manager_dispatch.py, we bind the real
powerChanged onto a bare harness and record the setpoint it hands to
power_charge / power_discharge instead of constructing a full manager.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from custom_components.zendure_ha.const import DeviceState, ManagerMode
from custom_components.zendure_ha.manager import ZendureManager


class _RecordingSensor:
    def __init__(self) -> None:
        self.values: list[Any] = []

    def update_value(self, value: Any) -> None:
        self.values.append(value)


class _Value(SimpleNamespace):
    def __init__(self, value: int) -> None:
        super().__init__(asInt=value, asNumber=value)


class _FuseGroup:
    def charge_limit(self, d: _FakeDevice) -> int:
        return d.pwr_max

    def discharge_limit(self, d: _FakeDevice) -> int:
        return d.pwr_max


class _FakeDevice:
    """Minimal stand-in exposing only what powerChanged reads."""

    def __init__(
        self,
        *,
        home_output: int = 0,
        home_input: int = 0,
        battery_output: int = 0,
        battery_input: int = 0,
        solar: int = 0,
        pwr_max: int = 800,
        state: DeviceState = DeviceState.INACTIVE,
        exports_bypass: bool = False,
    ) -> None:
        self.homeOutput = _Value(home_output)
        self.homeInput = _Value(home_input)
        self.batteryOutput = _Value(battery_output)
        self.batteryInput = _Value(battery_input)
        self.solarInput = _Value(solar)
        self.electricLevel = _Value(100 if state == DeviceState.SOCFULL else 50)
        self.pwr_max = pwr_max
        self.pwr_offgrid = 0
        self.pwr_produced = 0
        self.state = state
        self.exports_bypass = exports_bypass
        self.charge_optimal = 0
        self.discharge_optimal = 0
        self.actualKwh = 0
        self.kWh = 1
        self.fuseGrp = _FuseGroup()

    async def power_get(self) -> bool:
        return True


def _make_manager(bias: int, operation: ManagerMode = ManagerMode.MATCHING, devices: list[_FakeDevice] | None = None) -> ZendureManager:
    manager = object.__new__(ZendureManager)
    manager.devices = devices or []
    manager.operation = operation
    manager.p1bias = _Value(bias)
    manager.manualpower = _Value(0)
    manager.power = _RecordingSensor()
    manager.availableKwh = _RecordingSensor()
    manager.globalSoc = _RecordingSensor()
    manager.operationstate = _RecordingSensor()

    # state reset by _p1_changed before each powerChanged call
    manager.charge = []
    manager.charge_limit = 0
    manager.charge_optimal = 0
    manager.charge_weight = 0
    manager.discharge = []
    manager.discharge_bypass = 0
    manager.discharge_limit = 0
    manager.discharge_optimal = 0
    manager.discharge_produced = 0
    manager.discharge_weight = 0
    manager.idle = []
    manager.idle_lvlmax = 0
    manager.idle_lvlmin = 100
    manager.produced = 0

    manager.calls = []

    async def power_charge(setpoint: int, _time: datetime) -> None:
        manager.calls.append(("charge", setpoint))

    async def power_discharge(setpoint: int) -> None:
        manager.calls.append(("discharge", setpoint))

    manager.power_charge = power_charge
    manager.power_discharge = power_discharge
    return manager


async def _run(manager: ZendureManager, p1: int) -> list[tuple[str, int]]:
    await manager.powerChanged(p1, False, datetime.now())
    return manager.calls


async def test_zero_bias_keeps_setpoint_unchanged() -> None:
    assert await _run(_make_manager(0), 300) == [("discharge", 300)]


async def test_bias_reduces_battery_discharge() -> None:
    assert await _run(_make_manager(30), 300) == [("discharge", 270)]


async def test_bias_never_flips_small_load_into_charging() -> None:
    # 20W load with a 30W bias must idle, not charge 10W from the grid
    assert await _run(_make_manager(30), 20) == [("discharge", 0)]


async def test_bias_does_not_reduce_charging_from_surplus() -> None:
    assert await _run(_make_manager(30), -300) == [("charge", -300)]


async def test_bias_does_not_hold_back_produced_solar() -> None:
    # house 300W, served by 290W solar + 10W battery: only the battery share is held back
    device = _FakeDevice(home_output=300, battery_output=10, solar=290)
    assert await _run(_make_manager(30, devices=[device]), 0) == [("discharge", 290)]


async def test_bias_applies_to_battery_share_next_to_solar() -> None:
    # house 400W, device outputs 300W (200W solar + 100W battery)
    device = _FakeDevice(home_output=300, battery_output=100, solar=200)
    assert await _run(_make_manager(30, devices=[device]), 100) == [("discharge", 370)]


async def test_bias_ignored_for_full_device_with_curtailed_solar() -> None:
    # Full battery, feed-in forbidden: solarInput only shows the curtailed 370W.
    # The headroom up to pwr_max is solar too, so the house load of 400W is followed.
    device = _FakeDevice(home_output=370, solar=370, pwr_max=800, state=DeviceState.SOCFULL)
    assert await _run(_make_manager(30, devices=[device]), 30) == [("discharge", 400)]


async def test_bias_ignored_for_full_device_in_bypass() -> None:
    # Full battery, feed-in allowed: bypass is non-dispatchable and the setpoint is negative
    device = _FakeDevice(home_output=800, solar=800, state=DeviceState.SOCFULL, exports_bypass=True)
    assert await _run(_make_manager(30, devices=[device]), -500) == [("charge", -500)]


async def test_bias_in_mixed_setup_only_holds_back_beyond_full_device_capacity() -> None:
    # House 300W: the full device can deliver up to 290W of (curtailed) solar,
    # so only the remaining 10W battery share of the second device is held back.
    full = _FakeDevice(home_output=200, solar=200, pwr_max=290, state=DeviceState.SOCFULL)
    other = _FakeDevice(home_output=100, battery_output=100, pwr_max=800)
    assert await _run(_make_manager(30, devices=[full, other]), 0) == [("discharge", 290)]


async def test_store_solar_never_charges_from_grid() -> None:
    assert await _run(_make_manager(30, ManagerMode.STORE_SOLAR), -300) == [("charge", -300)]
    assert await _run(_make_manager(30, ManagerMode.STORE_SOLAR), 100) == [("charge", 0)]


async def test_manual_mode_ignores_bias() -> None:
    manager = _make_manager(30, ManagerMode.MANUAL)
    manager.manualpower = _Value(500)
    assert await _run(manager, 0) == [("discharge", 500)]


def test_bias_change_triggers_next_update() -> None:
    manager = object.__new__(ZendureManager)
    manager.zero_next = datetime.max

    manager.update_p1bias(None, 30)

    assert manager.zero_next == datetime.min
