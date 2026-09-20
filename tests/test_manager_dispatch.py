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

from custom_components.zendure_ha.const import DeviceState, ManagerState
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
    manager.charge_hold_start = datetime.max
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


async def test_power_charge_single_flip_keeps_full_holdoff() -> None:
    """A single discharge->charge flip shortly after a previous charge release
    still gets the full 60 s hold-off (unchanged behaviour)."""
    manager = _make_manager_harness()
    t0 = datetime(2026, 7, 24, 12, 0, 0)
    manager.charge_last = t0 - timedelta(seconds=30)

    await manager.power_charge(-500, t0)

    assert manager.charge_time == t0 + timedelta(seconds=60)
    assert manager.operationstate.values == [ManagerState.IDLE.value]


async def test_power_charge_holdoff_is_bounded_under_repeated_flips() -> None:
    """Regression for #1525: a grid point that keeps flipping faster than the
    hold-off must not re-arm a fresh 60 s window on every flip. The hold-off
    ends at most 60 s after the first arming of the episode."""
    manager = _make_manager_harness()
    t0 = datetime(2026, 7, 24, 12, 0, 0)
    manager.charge_last = t0 - timedelta(seconds=30)

    # flip pattern like a thermostat appliance or a wallbox following PV surplus
    await manager.power_charge(-500, t0)
    await manager.power_discharge(300)
    await manager.power_charge(-500, t0 + timedelta(seconds=35))
    assert manager.charge_time == t0 + timedelta(seconds=60)

    await manager.power_discharge(300)
    await manager.power_charge(-500, t0 + timedelta(seconds=55))
    assert manager.charge_time == t0 + timedelta(seconds=60)
    assert manager.operationstate.values[-1] == ManagerState.IDLE.value

    # after the bound has passed, the next flip releases charging immediately
    await manager.power_discharge(300)
    await manager.power_charge(-500, t0 + timedelta(seconds=70))
    assert manager.operationstate.values[-1] == ManagerState.CHARGE.value
    assert manager.charge_hold_start == datetime.max


async def test_power_charge_new_episode_after_release_gets_new_holdoff() -> None:
    """Once charging was released, the next flip starts a new episode with a
    fresh hold-off, so flapping is still damped."""
    manager = _make_manager_harness()
    t0 = datetime(2026, 7, 24, 12, 0, 0)
    manager.charge_last = t0 - timedelta(seconds=30)

    await manager.power_charge(-500, t0)
    await manager.power_charge(-500, t0 + timedelta(seconds=60))
    assert manager.operationstate.values[-1] == ManagerState.CHARGE.value

    await manager.power_discharge(300)
    t1 = t0 + timedelta(seconds=90)
    await manager.power_charge(-500, t1)
    assert manager.charge_time == t1 + timedelta(seconds=60)
    assert manager.operationstate.values[-1] == ManagerState.IDLE.value
    