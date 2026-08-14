"""Test harness driving the REAL ZendureManager against a fake device.

"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from custom_components.zendure_ha.const import DeviceState, ManagerMode
from custom_components.zendure_ha.manager import ZendureManager

CSV_DIR = Path(__file__).resolve().parent / "data"

# SoC label (spec) -> concrete DeviceState + a representative electricLevel %.
SOC = {
    "EMPTY": (DeviceState.SOCEMPTY, 5),
    "FULL": (DeviceState.SOCFULL, 100),
    "not full": (DeviceState.INACTIVE, 50),
}
SOC_ALL = ("EMPTY", "FULL", "not full")  # expansion for `any` rows


def _sensor(value: float = 0) -> SimpleNamespace:
    """Minimal stand-in for a Zendure entity exposing .asInt / .asNumber."""
    return SimpleNamespace(asInt=int(value), asNumber=float(value))


def _set(sensor: SimpleNamespace, value: float) -> None:
    """Update both faces of a sensor stand-in at once."""
    sensor.asInt = int(value)
    sensor.asNumber = float(value)


def _recorder() -> SimpleNamespace:
    """Captures manager entity writes; exposes .value and .update_value."""
    rec = SimpleNamespace(value=None)

    def update_value(value: Any) -> bool:
        rec.value = value
        return True

    rec.update_value = update_value
    return rec


class FakeFuseGroup:
    """Single-device fuse group: mirrors FuseGroup.*_limit for one device."""

    def __init__(self, maxpower: int = 3600, minpower: int = -3600) -> None:
        self.maxpower = maxpower
        self.minpower = minpower
        self.initPower = True

    def discharge_limit(self, d: "FakeDevice") -> int:
        d.pwr_max = min(self.maxpower, d.discharge_limit)
        return d.pwr_max

    def charge_limit(self, d: "FakeDevice") -> int:
        d.pwr_max = max(self.minpower, d.charge_limit)
        return d.pwr_max


class FakeDevice:
    """Duck-typed device that plays the exact surface powerChanged/power_discharge use.

    ``power_discharge`` / ``power_charge`` apply a physical battery plant model so
    the resulting sensors reflect what real hardware would settle to for the
    commanded setpoint, PV and SoC.
    """

    def __init__(self, soc_state: DeviceState, level: int, pv: int,
                 discharge_limit: int = 1200, charge_limit: int = -1200,
                 min_output: int = 0) -> None:
        self.state = soc_state
        self.pv = pv
        self.discharge_limit = discharge_limit
        self.charge_limit = charge_limit
        self.discharge_optimal = discharge_limit // 4
        self.discharge_start = discharge_limit // 10
        self.charge_optimal = charge_limit // 4
        self.pwr_max = discharge_limit
        self.minOutput = min_output
        self.exports_bypass = True
        self.pwr_offgrid = 0
        self.pwr_produced = 0
        self.kWh = 2.0
        self.actualKwh = 1.0
        self.fuseGrp = FakeFuseGroup()

        self.solarInput = _sensor(pv)
        self.homeOutput = _sensor(0)
        self.homeInput = _sensor(0)
        self.batteryOutput = _sensor(0)   # packInputPower: battery -> out (discharge)
        self.batteryInput = _sensor(0)    # outputPackPower: into battery (charge)
        self.electricLevel = _sensor(level)
        self.byPass = _sensor(0)

        self.commands: list[tuple[str, int]] = []

    @property
    def online(self) -> bool:
        return True

    async def power_get(self) -> bool:
        return True  # state is fixed for the scenario

    def seed_spec(self, discharging: int, charging: int, device_to_grid: int) -> None:
        """Place the device at the spec's steady-state operating point."""
        _set(self.homeOutput, max(0, device_to_grid))
        _set(self.homeInput, max(0, -device_to_grid))
        _set(self.batteryOutput, discharging)
        _set(self.batteryInput, charging)
        _set(self.solarInput, self.pv)

    @property
    def net_to_home(self) -> int:
        """Net power the device delivers to the home bus (negative = drawing grid)."""
        return self.homeOutput.asInt - self.homeInput.asInt

    def _apply_net(self, target: int) -> None:
        """Unified battery plant: `target` = commanded NET power to the home bus
        (discharge > 0, charge < 0). One consistent physics for every mode:

          * solar always flows first; the battery makes up a discharge gap or
            absorbs whatever solar is left over (store), never wasting it;
          * grid is drawn only when the command asks for more than solar (T<0
            below -PV, or a discharge the battery can't reach);
          * FULL only bypasses solar to home; EMPTY can't discharge the battery.
        """
        pv = self.pv
        if self.state == DeviceState.SOCFULL:
            net, bat_in, bat_out = pv, 0, 0            # bypass only
        elif self.state == DeviceState.SOCEMPTY and target > pv:
            net, bat_in, bat_out = pv, 0, 0            # can't discharge past solar
        else:
            net = target
            bat_in = max(0, pv - target)               # surplus solar (+grid if T<0) stored
            bat_out = max(0, target - pv)              # battery covers the gap
        _set(self.homeOutput, max(0, net))
        _set(self.homeInput, max(0, -net))
        _set(self.batteryInput, bat_in)
        _set(self.batteryOutput, bat_out)

    async def power_discharge(self, power: int) -> int:
        out = max(0, min(power, self.discharge_limit))   # mirror device.power_discharge clamp
        self.commands.append(("discharge", power))
        self._apply_net(out)
        return self.homeOutput.asInt

    async def power_charge(self, power: int) -> int:
        chg = min(0, max(power, self.charge_limit))      # mirror device.power_charge clamp
        self.commands.append(("charge", power))
        self._apply_net(chg)
        return chg


def build_manager(mode: ManagerMode, device: FakeDevice) -> ZendureManager:
    mgr = object.__new__(ZendureManager)  # bypass HA-coupled __init__
    mgr.operation = mode
    mgr.devices = [device]
    mgr.simulation = False
    # manager entities -> recorders
    mgr.power = _recorder()
    mgr.availableKwh = _recorder()
    mgr.globalSoc = _recorder()
    mgr.operationstate = _recorder()
    # hysteresis / distribution state
    mgr.charge_time = datetime.max
    mgr.charge_last = datetime.min
    mgr.pwr_low = 0
    return mgr


async def run_step(mgr: ZendureManager, p1: int, time: datetime | None = None) -> None:
    """Reset per-cycle accumulators (as _p1_changed does) then run one real cycle.

    ``time`` drives the manager's charge hysteresis (``charge_time = time + 2s``);
    callers doing a settling loop must advance it by >2s per cycle or charging
    never releases.
    """
    if time is None:
        time = datetime.now()
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
    for d in mgr.devices:
        d.fuseGrp.initPower = True
    await mgr.powerChanged(p1, False, time)



async def drive_metered(mode: ManagerMode, case: "Case", cycles: int = 60) -> FakeDevice:
    """Faithful driver: seed the device at the spec state, then close the loop
    through a RESIDUAL P1 meter and run real ``powerChanged`` cycles.
    Assert the spec state is a stable equilibrium.

    MANUAL ignores P1 (uses manualpower); every other mode balances the load.
    """
    state, level = SOC[case.soc]
    charge_limit = -1200 if mode == ManagerMode.MANUAL else 0
    dev = FakeDevice(state, level, pv=case.pv, charge_limit=charge_limit)
    dev.seed_spec(case.discharging, case.charging, case.device_to_grid)
    mgr = build_manager(mode, dev)
    if mode == ManagerMode.MANUAL:
        mgr.manualpower = _sensor(case.p1)          # input_w is the manual power
    load = 0 if mode == ManagerMode.MANUAL else case.p1
    base = datetime(2026, 1, 1, 0, 0, 0)
    for i in range(cycles):
        # advance wall-clock >2s/cycle so the charge hysteresis releases
        await run_step(mgr, load - dev.net_to_home, base + timedelta(seconds=120 * i))
    return dev


@dataclass
class Case:
    mode: str
    num: int
    p1: int
    pv: int
    soc: str          # concrete SoC label (any-rows already expanded)
    discharging: int
    charging: int
    device_to_grid: int
    notes: str
    any_row: bool = False

    @property
    def id(self) -> str:
        star = "*" if self.any_row else ""
        return f"{self.mode}-r{self.num}-p1={self.p1}-pv={self.pv}-{self.soc}{star}"


def make_params(cases: list["Case"]) -> list:
    """Build pytest params from Case list."""
    import pytest

    return [pytest.param(c, id=c.id) for c in cases]


def load_cases_from_csv(mode_stem: str) -> list[Case]:
    """Load a mode CSV, expanding `any` rows into the three concrete SoC states."""
    cases: list[Case] = []
    with (CSV_DIR / f"{mode_stem}.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            soc = row["soc"].strip()
            base = dict(
                mode=row["mode"],
                num=int(row["case"]),
                p1=int(row["input_w"]),
                pv=int(row["pv_w"]),
                discharging=int(row["battery_discharging_w"]),
                charging=int(row["battery_charging_w"]),
                device_to_grid=int(row["device_to_grid_w"]),
                notes=row["notes"],
            )
            if soc == "any":
                for s in SOC_ALL:
                    cases.append(Case(soc=s, any_row=True, **base))
            else:
                cases.append(Case(soc=soc, **base))
    return cases
