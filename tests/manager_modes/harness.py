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
    """Fuse group stand-in mirroring FuseGroup.*_limit for one or many devices."""

    def __init__(self, maxpower: int = 3600, minpower: int = -3600,
                 devices: list["FakeDevice"] | None = None) -> None:
        self.maxpower = maxpower
        self.minpower = minpower
        self.initPower = True
        self.devices: list[FakeDevice] = devices if devices is not None else []
        for d in self.devices:
            d.fuseGrp = self

    def discharge_limit(self, d: "FakeDevice") -> int:
        if self.initPower:
            self.initPower = False
            if len(self.devices) == 1:
                d.pwr_max = min(self.maxpower, d.discharge_limit)
            else:
                limit = 0
                weight = 0
                for fd in self.devices:
                    if fd.homeOutput.asInt > 0:
                        limit += fd.discharge_limit
                        weight += fd.electricLevel.asInt * fd.discharge_limit
                avail = min(self.maxpower, limit)
                for fd in self.devices:
                    if fd.homeOutput.asInt > 0:
                        fd.pwr_max = int(avail * (fd.electricLevel.asInt * fd.discharge_limit) / weight) if weight > 0 else fd.discharge_start
                        limit -= fd.discharge_limit
                        if limit < avail - fd.pwr_max:
                            fd.pwr_max = min(avail - limit, avail)
                        fd.pwr_max = min(fd.pwr_max, fd.discharge_limit)
                        avail -= fd.pwr_max
        return d.pwr_max

    def charge_limit(self, d: "FakeDevice") -> int:
        if self.initPower:
            self.initPower = False
            if len(self.devices) == 1:
                d.pwr_max = max(self.minpower, d.charge_limit)
            else:
                limit = 0
                weight = 0
                for fd in self.devices:
                    if fd.homeInput.asInt > 0:
                        limit += fd.charge_limit
                        weight += (100 - fd.electricLevel.asInt) * fd.charge_limit
                avail = max(self.minpower, limit)
                for fd in self.devices:
                    if fd.homeInput.asInt > 0:
                        fd.pwr_max = int(avail * ((100 - fd.electricLevel.asInt) * fd.charge_limit) / weight) if weight < 0 else fd.charge_start
                        limit -= fd.charge_limit
                        if limit > avail - fd.pwr_max:
                            fd.pwr_max = max(avail - limit, avail)
                        fd.pwr_max = max(fd.pwr_max, fd.charge_limit)
                        avail -= fd.pwr_max
        return d.pwr_max


class FakeDevice:
    """Duck-typed device that plays the exact surface powerChanged/power_discharge use.

    ``power_discharge`` / ``power_charge`` apply a physical battery plant model so
    the resulting sensors reflect what real hardware would settle to for the
    commanded setpoint, PV and SoC.
    """

    def __init__(self, soc_state: DeviceState, level: int, pv: int,
                 discharge_limit: int = 1200, charge_limit: int = -1200,
                 pwr_offgrid: int = 0, exports_bypass: bool = True,
                 name: str = "dev") -> None:
        self.name = name
        self.state = soc_state
        self.pv = pv
        self.discharge_limit = discharge_limit
        self.charge_limit = charge_limit
        self.discharge_optimal = discharge_limit // 4
        self.discharge_start = discharge_limit // 10
        self.charge_optimal = charge_limit // 4
        self.charge_start = charge_limit // 10
        self.pwr_max = discharge_limit
        self.exports_bypass = exports_bypass
        self.pwr_offgrid = pwr_offgrid
        self.pwr_produced = 0
        self.kWh = 2.0
        self.actualKwh = 1.0
        self.fuseGrp = FakeFuseGroup(devices=[self])

        self.solarInput = _sensor(pv)
        self.homeOutput = _sensor(0)
        self.homeInput = _sensor(0)
        self.batteryOutput = _sensor(0)   # packInputPower: battery -> out (discharge)
        self.batteryInput = _sensor(0)    # outputPackPower: into battery (charge)
        self.electricLevel = _sensor(level)
        # a full battery passing its solar reports hardware bypass, like real devices
        self.byPass = _sensor(1 if (soc_state == DeviceState.SOCFULL and pv > 0) else 0)

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
        """Unified battery plant: `target` = commanded NET power to the home
        (discharge > 0, charge < 0). Cconsistent for every mode:

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


def build_manager(mode: ManagerMode, devices: list[FakeDevice],
                  fuse: int | None = None) -> ZendureManager:
    if fuse is not None:
        FakeFuseGroup(maxpower=fuse, minpower=-fuse, devices=devices)
    mgr = object.__new__(ZendureManager)  # bypass HA-coupled __init__
    mgr.operation = mode
    mgr.devices = devices
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
    for grp in {d.fuseGrp for d in mgr.devices}:
        grp.initPower = True
    await mgr.powerChanged(p1, False, time)



async def drive_metered(mode: ManagerMode, case: "Case", cycles: int = 60) -> list[FakeDevice]:
    """Faithful driver: seed each device at its spec state, then close the loop
    through a RESIDUAL P1 meter and run real ``powerChanged`` cycles.
    Assert the spec state is a stable equilibrium.

    MANUAL ignores P1 (uses manualpower); every other mode balances the load.
    """
    charge_limit = -1200 if mode == ManagerMode.MANUAL else 0
    devs = []
    for spec in case.devices:
        state, rep_level = SOC[spec.soc]
        dev = FakeDevice(state, spec.level if spec.level is not None else rep_level,
                         pv=spec.pv, charge_limit=charge_limit,
                         pwr_offgrid=spec.offgrid, exports_bypass=spec.exports,
                         name=f"dev{len(devs) + 1}")
        dev.seed_spec(spec.discharging, spec.charging, spec.device_to_grid)
        devs.append(dev)
    mgr = build_manager(mode, devs, case.fuse)
    if mode == ManagerMode.MANUAL:
        mgr.manualpower = _sensor(case.p1)          # input_w is the manual power
    load = 0 if mode == ManagerMode.MANUAL else case.p1
    base = datetime(2026, 1, 1, 0, 0, 0)
    for i in range(cycles):
        # advance wall-clock >2s/cycle so the charge hysteresis releases
        total_net = sum(d.net_to_home for d in devs)
        await run_step(mgr, load - total_net, base + timedelta(seconds=120 * i))
    return devs


@dataclass
class DeviceSpec:
    """One device's inputs and expected steady-state outputs for a case."""

    pv: int
    soc: str
    level: int | None
    discharging: int
    charging: int
    device_to_grid: int
    offgrid: int = 0        # off-grid consumers on this device, W (0 = none)
    exports: bool = True    # exports_bypass: gridReverse allows export


@dataclass
class Case:
    mode: str
    num: int
    p1: int
    fuse: int | None       # shared group maxpower; None = per-device groups
    devices: list[DeviceSpec]
    notes: str
    any_row: bool = False

    @property
    def id(self) -> str:
        star = "*" if self.any_row else ""
        d0, *rest = self.devices
        if not rest:
            return f"{self.num}:{self.mode}:p1={self.p1}:pv={d0.pv}:{d0.soc}{star}"
        d1 = rest[0]
        lv = f"lv={d0.level}/{d1.level}:" if d0.level is not None or d1.level is not None else ""
        return f"{self.num}:{self.mode}:p1={self.p1}:{lv}{d0.soc}:{d1.soc}{star}"


def assert_matches_spec(devs: list[FakeDevice], case: "Case") -> None:
    """Assert each device's steady state matches its spec.

    On failure, prints an expected-vs-actual comparison table (pytest shows
    captured stdout in the failure section) and raises one AssertionError
    carrying the case id and notes.
    """
    table = []
    failed = False
    for i, (dev, spec) in enumerate(zip(devs, case.devices), 1):
        actual = (dev.batteryOutput.asInt, dev.batteryInput.asInt, dev.net_to_home)
        expected = (spec.discharging, spec.charging, spec.device_to_grid)
        ok = actual == expected
        failed |= not ok
        table.append((f"dev{i}", spec.pv, expected, actual, ok))

    if failed:
        print(f"[case {case.num} {case.mode}] {case.notes}")
        print(f"{'device':>7} | {'PV':>5} | {'D exp':>6} {'C exp':>6} {'G exp':>6} | {'D act':>6} {'C act':>6} {'G act':>6}")
        for name, pv, (de, ce, ge), (da, ca, ga), ok in table:
            mark = "" if ok else "  <-- mismatch"
            print(f"{name:>7} | {pv:>5} | {de:>6} {ce:>6} {ge:>6} | {da:>6} {ca:>6} {ga:>6}{mark}")

    assert not failed, f"[{case.mode} case #{case.num}]"


def make_params(cases: list["Case"]) -> list:
    """Build pytest params from Case list."""
    import pytest

    return [pytest.param(c, id=c.id) for c in cases]


def load_cases_from_csv(mode_stem: str) -> list[Case]:
    """Load a mode CSV, expanding `any` rows per device (cartesian product)."""
    cases: list[Case] = []
    with (CSV_DIR / f"{mode_stem}.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            soc = row["soc"].strip()

            def _level(cell: str | None) -> int | None:
                if cell is None or cell.strip() == "":
                    return None
                return int(cell)

            def _offgrid(cell: str | None) -> int:
                return int(cell) if (cell or "").strip() else 0

            def _exports(cell: str | None) -> bool:
                return not ((cell or "").strip() == "0")

            base = dict(
                mode=row["mode"],
                num=int(row["case"]),
                p1=int(row["input_w"]),
                fuse=int(row["fuse_w"]) if (row.get("fuse_w") or "").strip() else None,
                notes=row["notes"],
            )
            spec1 = DeviceSpec(
                pv=int(row["pv_w"]),
                soc=soc,
                level=_level(row.get("level")),
                discharging=int(row["battery_discharging_w"]),
                charging=int(row["battery_charging_w"]),
                device_to_grid=int(row["device_to_grid_w"]),
                offgrid=_offgrid(row.get("offgrid_w")),
                exports=_exports(row.get("exports")),
            )
            soc2 = (row.get("soc2") or "").strip()
            if soc2 == "":
                for s in (SOC_ALL if soc == "any" else (soc,)):
                    cases.append(Case(
                        devices=[DeviceSpec(pv=spec1.pv, soc=s, level=spec1.level,
                                           discharging=spec1.discharging, charging=spec1.charging,
                                           device_to_grid=spec1.device_to_grid,
                                           offgrid=spec1.offgrid, exports=spec1.exports)],
                        any_row=soc == "any",
                        **base,
                    ))
                continue
            spec2 = DeviceSpec(
                pv=int(row["pv2_w"]),
                soc=soc2,
                level=_level(row.get("level2")),
                discharging=int(row["battery2_discharging_w"]),
                charging=int(row["battery2_charging_w"]),
                device_to_grid=int(row["device2_to_grid_w"]),
                offgrid=_offgrid(row.get("offgrid2_w")),
                exports=_exports(row.get("exports2")),
            )
            socs1 = SOC_ALL if soc == "any" else (soc,)
            socs2 = SOC_ALL if soc2 == "any" else (soc2,)
            for s1 in socs1:
                for s2 in socs2:
                    cases.append(Case(
                        devices=[
                            DeviceSpec(pv=spec1.pv, soc=s1, level=spec1.level,
                                       discharging=spec1.discharging, charging=spec1.charging,
                                       device_to_grid=spec1.device_to_grid,
                                       offgrid=spec1.offgrid, exports=spec1.exports),
                            DeviceSpec(pv=spec2.pv, soc=s2, level=spec2.level,
                                       discharging=spec2.discharging, charging=spec2.charging,
                                       device_to_grid=spec2.device_to_grid,
                                       offgrid=spec2.offgrid, exports=spec2.exports),
                        ],
                        any_row=soc == "any" or soc2 == "any",
                        **base,
                    ))
    return cases
