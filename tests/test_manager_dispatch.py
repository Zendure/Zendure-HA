"""Scenario tests for ZendureManager.power_charge / power_discharge dispatch.

ZendureManager itself is heavyweight to construct (DataUpdateCoordinator +
EntityDevice + a real config entry, P1 meter wiring, etc.), but power_charge/
power_discharge are self-contained methods that only touch a well-defined set
of `self.*` attributes. We bind the real methods onto a bare harness object
and set exactly the state they read, instead of constructing a full manager -
this exercises the real dispatch algorithm without the unrelated setup cost.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, call

import pytest

from custom_components.zendure_ha.const import DeviceState, ManagerMode, ManagerState
from custom_components.zendure_ha.manager import ZendureManager


class _RecordingSensor:
    def __init__(self) -> None:
        self.values: list[Any] = []

    def update_value(self, value: Any) -> None:
        self.values.append(value)


class _FakeDevice:
    """Minimal stand-in exposing only what power_charge/power_discharge touch."""

    def __init__(self, *, pwr_max: int, electric_level: int, state: DeviceState = DeviceState.INACTIVE) -> None:
        self.pwr_max = pwr_max
        self.electricLevel = SimpleNamespace(asInt=electric_level)
        self.byPass = SimpleNamespace(asInt=0)
        self.pwr_offgrid = 0
        self.pwr_produced = 0
        self.state = state
        self.charge_start = 0
        self.charge_optimal = 0
        self.discharge_start = 0
        self.discharge_optimal = 0
        self.charge_calls: list[int] = []
        self.discharge_calls: list[int] = []

    async def power_charge(self, power: int) -> int:
        self.charge_calls.append(power)
        return power

    async def power_discharge(self, power: int) -> int:
        self.discharge_calls.append(power)
        return power


def _make_manager_harness() -> ZendureManager:
    manager = object.__new__(ZendureManager)
    manager.operationstate = _RecordingSensor()
    manager.charge = []
    manager.charge_limit = 0
    manager.charge_optimal = 0
    manager.charge_time = datetime.max
    manager.charge_last = datetime.min
    manager.charge_weight = 0
    manager.discharge = []
    manager.discharge_produced = 0
    manager.discharge_limit = 0
    manager.discharge_optimal = 0
    manager.discharge_weight = 0
    manager.idle = []
    manager.idle_lvlmax = 0
    manager.idle_lvlmin = 0
    manager.pwr_low = 0
    return manager


def _make_recent_flip_harness(now: datetime) -> tuple[ZendureManager, _FakeDevice]:
    """One half-full charging device; the last hold-off window ended 30s ago."""
    manager = _make_manager_harness()
    device = _FakeDevice(pwr_max=-2400, electric_level=50)
    manager.charge = [device]
    manager.charge_limit = -2400
    manager.charge_weight = device.pwr_max * (100 - device.electricLevel.asInt)
    manager.charge_last = now - timedelta(seconds=30)
    return manager, device


async def test_power_discharge_single_device_gets_full_setpoint() -> None:
    manager = _make_manager_harness()
    device = _FakeDevice(pwr_max=2400, electric_level=50)
    manager.discharge = [device]
    manager.discharge_limit = 2400
    manager.discharge_weight = device.pwr_max * device.electricLevel.asInt

    await manager.power_discharge(500)

    assert device.discharge_calls == [500]
    assert manager.operationstate.values == [ManagerState.DISCHARGE.value]


async def test_power_discharge_no_devices_sets_idle_state() -> None:
    manager = _make_manager_harness()

    await manager.power_discharge(0)

    assert manager.operationstate.values == [ManagerState.IDLE.value]


async def test_power_charge_first_call_primes_hysteria_and_forces_zero_setpoint() -> None:
    """The 'prevent hysteria' branch always fires on the first call (charge_time
    starts at datetime.max), forcing this call's setpoint to 0 and scheduling
    charge_time into the near future rather than dispatching real charge power.
    """
    manager = _make_manager_harness()
    now = datetime(2026, 7, 24, 12, 0, 0)
    manager.charge_last = now - timedelta(minutes=10)

    await manager.power_charge(-500, now)

    assert manager.charge_time == now + timedelta(seconds=2)
    assert manager.pwr_low == 0
    assert manager.operationstate.values == [ManagerState.IDLE.value]


async def test_power_charge_recent_flip_holds_off_for_60s() -> None:
    """A flip less than 300s after the previous hold-off window ended arms the long
    (60s) hold-off and clamps the setpoint to 0: no charge command reaches the device.
    """
    now = datetime(2026, 7, 24, 12, 0, 0)
    manager, device = _make_recent_flip_harness(now)

    await manager.power_charge(-2400, now)

    assert manager.charge_time == now + timedelta(seconds=60)
    assert device.charge_calls == [0]
    assert manager.operationstate.values == [ManagerState.IDLE.value]


async def test_power_charge_without_debounce_skips_hold_off() -> None:
    """With debounce=False, the same state that arms the 60s hold-off above dispatches
    the requested power right away and records the flip as already elapsed.
    """
    now = datetime(2026, 7, 24, 12, 0, 0)
    manager, device = _make_recent_flip_harness(now)

    await manager.power_charge(-2400, now, debounce=False)

    assert manager.charge_time == now
    assert device.charge_calls == [-2400]
    assert manager.operationstate.values == [ManagerState.CHARGE.value]


async def test_power_charge_without_debounce_clears_pending_hold_off() -> None:
    """A non-debounced charge (e.g. switching to MANUAL) during a pending hold-off
    charges right away, and a later debounced charge (back to MATCHING) does not
    inherit the old hold-off window.
    """
    now = datetime(2026, 7, 24, 12, 0, 0)
    manager, device = _make_recent_flip_harness(now)
    weight = manager.charge_weight

    for seconds, debounce in ((0, True), (5, False), (20, True)):
        manager.charge_weight = weight  # powerChanged recomputes it every cycle
        time = now + timedelta(seconds=seconds)
        await manager.power_charge(-2400, time, debounce=debounce)

    assert device.charge_calls == [0, -2400, -2400]


_NOW = datetime(2026, 7, 24, 12, 0, 0)


@pytest.mark.parametrize(
    ("operation", "p1", "produced", "manual", "charge_calls", "discharge_calls"),
    [
        (ManagerMode.MATCHING, -500, 0, 0, [call(-500, _NOW)], []),
        (ManagerMode.MATCHING_CHARGE, -500, 0, 0, [call(-500, _NOW)], []),
        (ManagerMode.MATCHING_CHARGE, 500, 300, 0, [], [call(300)]),
        (ManagerMode.MATCHING_CHARGE, 500, 0, 0, [call(0, _NOW)], []),
        (ManagerMode.STORE_SOLAR, 500, 300, 0, [call(0, _NOW, debounce=False)], []),
        (ManagerMode.STORE_SOLAR, -500, 0, 0, [call(-500, _NOW, debounce=False)], []),
        (ManagerMode.MANUAL, 500, 0, -800, [call(-800, _NOW, debounce=False)], []),
        (ManagerMode.MANUAL, -500, 0, 800, [], [call(800)]),
    ],
    ids=[
        "matching-charge",
        "matching_charge-charge",
        "matching_charge-discharges-produced",
        "matching_charge-idle",
        "store_solar-never-discharges",
        "store_solar-charge",
        "manual-charge",
        "manual-discharge",
    ],
)
async def test_power_changed_debounces_only_matching_modes(
    operation: ManagerMode,
    p1: int,
    produced: int,
    manual: int,
    charge_calls: list[Any],
    discharge_calls: list[Any],
) -> None:
    """powerChanged keeps the charge hold-off for the MATCHING modes, which can flip on
    their own, and disables it for MANUAL and STORE_SOLAR, whose direction is fixed.
    """
    manager = _make_manager_harness()
    manager.devices = []
    manager.power = _RecordingSensor()
    manager.availableKwh = _RecordingSensor()
    manager.globalSoc = _RecordingSensor()
    manager.discharge_bypass = 0
    manager.produced = produced
    manager.operation = operation
    manager.manualpower = SimpleNamespace(asNumber=manual)
    manager.power_charge = AsyncMock()
    manager.power_discharge = AsyncMock()

    await manager.powerChanged(p1, False, _NOW)

    assert manager.power_charge.await_args_list == charge_calls
    assert manager.power_discharge.await_args_list == discharge_calls
