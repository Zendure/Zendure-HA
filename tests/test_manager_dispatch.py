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
    manager.charge_weight = 0
    manager.discharge = []
    manager.discharge_bypass = 0
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


def _make_socfull_bypass_scenario() -> tuple[ZendureManager, _FakeDevice, _FakeDevice, _FakeDevice]:
    """Real-world capture: 3-device fleet, 2026-08-19 15:30 CEST.

    One SOCFULL Hyper 2000 bypasses 590 W of its own solar to the home. The two
    other devices are charging from their *own DC solar* (not from the home bus),
    so homeInput/homeOutput are both 0 and powerChanged() files them under `idle`,
    not `charge` -- nothing in power_discharge stops their charging.

    House load 894 W, total PV 1172 W, 304 W bought from the grid.
    """
    manager = _make_manager_harness()

    full = _FakeDevice(pwr_max=1200, electric_level=100, state=DeviceState.SOCFULL)
    full.pwr_produced = -590
    full.discharge_start = 120
    full.discharge_optimal = 300

    idle_high = _FakeDevice(pwr_max=1200, electric_level=93)
    idle_high.pwr_produced = -398
    idle_high.discharge_start = 120
    idle_high.discharge_optimal = 300

    idle_low = _FakeDevice(pwr_max=800, electric_level=59)
    idle_low.pwr_produced = -145
    idle_low.discharge_start = 80
    idle_low.discharge_optimal = 200

    manager.discharge = [full]
    manager.discharge_limit = 1200
    manager.discharge_optimal = full.discharge_optimal
    manager.discharge_produced = 590
    manager.discharge_bypass = 590
    manager.discharge_weight = full.pwr_max * full.electricLevel.asInt
    manager.idle = [idle_high, idle_low]
    manager.idle_lvlmax = 93
    manager.idle_lvlmin = 59
    return manager, full, idle_high, idle_low


async def test_socfull_bypass_is_never_commanded_below_its_production() -> None:
    """A SOCFULL device's solar bypass is non-dispatchable. Commanding it below
    what it already produces curtails PV and pushes the deficit onto the grid.
    """
    manager, full, _idle_high, _idle_low = _make_socfull_bypass_scenario()

    await manager.power_discharge(304)

    assert full.discharge_calls, "the bypassing device got no command at all"
    assert full.discharge_calls[-1] >= 590, (
        f"SOCFULL device commanded {full.discharge_calls[-1]} W while already producing 590 W: "
        "its solar gets curtailed and the grid import grows"
    )


async def test_socfull_bypass_does_not_mask_a_real_deficit_from_idle_devices() -> None:
    """304 W of real deficit remains after the bypass. The SOCFULL device cannot
    serve it (battery full), so an idle device must be started.
    """
    manager, _full, idle_high, idle_low = _make_socfull_bypass_scenario()

    await manager.power_discharge(304)

    assert idle_high.discharge_calls or idle_low.discharge_calls, (
        "no idle device was started: the 304 W deficit stays on the grid even though "
        "two devices with charged batteries and their own solar are sitting idle"
    )


def _make_socempty_scenario() -> tuple[ZendureManager, _FakeDevice, _FakeDevice, _FakeDevice]:
    """Real-world capture: same fleet, 2026-08-20 09:06 CEST, the mirror case.

    Both Hyper 2000 sit at minSoc (5%) -> SOCEMPTY: they still pass their own solar
    to the home (146 W and 65 W) so powerChanged() files them under `discharge`,
    but their batteries cannot supply anything. The SolarFlow is at 7%, has real
    reserve left, and is charging from its own DC solar -> filed under `idle`.

    House 370 W of target output, 211 W of solar passing through, 159 W bought.
    """
    manager = _make_manager_harness()

    empty_a = _FakeDevice(pwr_max=1200, electric_level=5, state=DeviceState.SOCEMPTY)
    empty_a.pwr_produced = -146
    empty_a.discharge_start = 120
    empty_a.discharge_optimal = 300

    empty_b = _FakeDevice(pwr_max=1200, electric_level=5, state=DeviceState.SOCEMPTY)
    empty_b.pwr_produced = -65
    empty_b.discharge_start = 120
    empty_b.discharge_optimal = 300

    reserve = _FakeDevice(pwr_max=800, electric_level=7)
    reserve.pwr_produced = -55
    reserve.discharge_start = 80
    reserve.discharge_optimal = 200

    manager.discharge = [empty_a, empty_b]
    manager.discharge_limit = 2400
    manager.discharge_optimal = empty_a.discharge_optimal + empty_b.discharge_optimal
    manager.discharge_produced = 211
    manager.discharge_bypass = 0
    manager.discharge_weight = sum(d.pwr_max * d.electricLevel.asInt for d in manager.discharge)
    manager.idle = [reserve]
    manager.idle_lvlmax = 7
    manager.idle_lvlmin = 7
    return manager, empty_a, empty_b, reserve


async def test_socempty_devices_do_not_mask_a_real_deficit() -> None:
    """Devices pinned at minSoc have no dispatchable headroom either. Crediting
    them with discharge_optimal cancels the 159 W deficit, so the one device that
    still holds usable charge is never started and the grid pays instead.
    """
    manager, _a, _b, reserve = _make_socempty_scenario()

    await manager.power_discharge(370)

    assert reserve.discharge_calls, (
        "the only device with charge left was not started: the 159 W deficit stays on "
        "the grid while two SOCEMPTY devices are credited with 600 W of phantom headroom"
    )
