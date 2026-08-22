"""SoC boundary tests — socSet, minSoc thresholds and the socSet=0 kill-switch.

These are mode-agnostic: they test classification and distribution at SoC
boundaries. MATCHING mode is used as the representative mode. The device fake
mirrors the real ZendureDevice.power_get() SoC classification so the tests
exercise the manager's boundary handling end to end.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from custom_components.zendure_ha.const import DeviceState, ManagerMode, PowerFlowDirection
from custom_components.zendure_ha.manager import ZendureManager

MODE = ManagerMode.MATCHING


def _sensor(value: float = 0) -> SimpleNamespace:
    return SimpleNamespace(asInt=int(value), asNumber=float(value))


def _recorder() -> SimpleNamespace:
    rec = SimpleNamespace(value=None)

    def update_value(value: Any) -> bool:
        rec.value = value
        return True

    rec.update_value = update_value
    return rec


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
    """Minimal device exposing what powerChanged touches, with real SoC classification."""

    def __init__(
        self,
        *,
        electric_level: int,
        state: DeviceState,
        home_output: int = 0,
        soc_set: float = 100,
        min_soc: float = 0,
    ) -> None:
        self.name = "dev"
        self.state = state
        self.electricLevel = _sensor(electric_level)
        self.homeOutput = _sensor(home_output)
        self.homeInput = _sensor(0)
        self.batteryInput = _sensor(0)
        self.batteryOutput = _sensor(0)
        self.solarInput = _sensor(0)
        self.byPass = _sensor(0)
        self.socSet = _sensor(soc_set)
        self.minSoc = _sensor(min_soc)
        self.pwr_max = 1200
        self.pwr_offgrid = 0
        self.pwr_produced = 0
        self.exports_bypass = True
        self.kWh = 10.0
        self.actualKwh = 10.0
        self.min_output = 0
        self.awake = False
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

    def on_direction_change(self, direction: PowerFlowDirection) -> None:
        self.awake = direction != PowerFlowDirection.CHARGE

    async def power_get(self) -> bool:
        # Mirrors ZendureDevice.power_get() classification (socLimit always 0 here).
        if self.socSet.asNumber == 0:
            self.state = DeviceState.OFFLINE
        elif self.electricLevel.asInt >= self.socSet.asNumber:
            self.state = DeviceState.SOCFULL
        elif self.electricLevel.asInt <= self.minSoc.asNumber:
            self.state = DeviceState.SOCEMPTY
        else:
            self.state = DeviceState.INACTIVE
        return self.state != DeviceState.OFFLINE

    async def power_discharge(self, power: int) -> int:
        self.discharge_calls.append(power)
        return power

    async def power_charge(self, power: int) -> int:
        self.charge_calls.append(power)
        return power


def _manager_harness(operation: ManagerMode, device: _FakeDevice) -> ZendureManager:
    """Bare ZendureManager bound to the real methods, like upstream's test_manager_dispatch."""
    mgr = object.__new__(ZendureManager)
    mgr.operation = operation
    mgr.devices = [device]
    mgr.simulation = False
    mgr.power = _recorder()
    mgr.availableKwh = _recorder()
    mgr.globalSoc = _recorder()
    mgr.operationstate = _recorder()
    mgr.manualpower = _sensor(0)
    mgr.charge_time = datetime.max
    mgr.charge_last = datetime.min
    mgr.pwr_low = 0
    # per-cycle accumulators (reset by _p1_changed in production)
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


async def test_socset_80_elevel_79_is_not_full() -> None:
    """S1: socSet=80, eLevel=79 → INACTIVE (79 < 80, not full), normal discharge."""
    device = _FakeDevice(state=DeviceState.INACTIVE, home_output=200, electric_level=79, soc_set=80)
    mgr = _manager_harness(MODE, device)

    await mgr.powerChanged(100, False, datetime.now())

    assert device.discharge_calls != []


async def test_minsoc_20_elevel_20_is_empty() -> None:
    """S2: minSoc=20, eLevel=20 → SOCEMPTY (<= boundary, exactly at threshold)."""
    device = _FakeDevice(state=DeviceState.SOCEMPTY, electric_level=20, min_soc=20)
    mgr = _manager_harness(MODE, device)

    await mgr.powerChanged(100, False, datetime.now())

    assert device.discharge_calls == []


async def test_minsoc_20_elevel_21_is_not_empty() -> None:
    """S3: minSoc=20, eLevel=21 → INACTIVE (21 > 20, not empty), kickstarted."""
    device = _FakeDevice(state=DeviceState.INACTIVE, electric_level=21, min_soc=20)
    mgr = _manager_harness(MODE, device)

    await mgr.powerChanged(100, False, datetime.now())

    assert device.discharge_calls == [50]


async def test_socset_zero_device_offline() -> None:
    """S4: socSet=0 → OFFLINE, device skipped entirely (kill-switch)."""
    device = _FakeDevice(state=DeviceState.INACTIVE, home_output=300, electric_level=50, soc_set=0)
    mgr = _manager_harness(MODE, device)

    await mgr.powerChanged(100, False, datetime.now())

    assert device.discharge_calls == []
    assert device.charge_calls == []


async def test_minsoc_20_elevel_30_weighting_skew() -> None:
    """S5: minSoc=20, eLevel=30 → INACTIVE; idle-start kickstart fires (50W)."""
    device = _FakeDevice(state=DeviceState.INACTIVE, electric_level=30, min_soc=20)
    mgr = _manager_harness(MODE, device)

    await mgr.powerChanged(100, False, datetime.now())

    assert device.discharge_calls == [50]


async def test_minsoc_20_elevel_20_manual_discharge_blocked() -> None:
    """S6: SOCEMPTY at 20% with manual discharge → blocked."""
    device = _FakeDevice(state=DeviceState.SOCEMPTY, electric_level=20, min_soc=20)
    mgr = _manager_harness(ManagerMode.MANUAL, device)
    mgr.manualpower = _sensor(300)

    await mgr.powerChanged(0, False, datetime.now())

    assert device.discharge_calls == []
