"""SoC balancing (soc_balancing) — the battery budget tilts toward one
device, so the group converges fastest.

Discharging: floors first, then the remaining budget blends the default
weighted split with the strict 100/0 handover to the highest-SoC device,
tilted by the SoC spread (0 at SOC_SPREAD_MIN, 1 at
SOC_SPREAD_FULL). Charging mirrors it toward the lowest-SoC
device. When every battery's SoC is within SOC_SPREAD_MIN % of the
others, the split falls back to the default proportional one.

Behaviour behind ZendureManager.soc_balancing (default True). This
module flips the flag on for every case in data/soc_balancing.csv, so the
default SoC-weighted splits stay pinned by the other suites untouched.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.zendure_ha.const import DeviceState, ManagerMode
from custom_components.zendure_ha.manager import ZendureManager

from .harness import Case, assert_matches_spec, drive_metered, load_cases_from_csv, make_params
from .test_min_output import _FakeDevice, _manager

CASES = load_cases_from_csv("soc_balancing")


@pytest.fixture(autouse=True)
def enable_balancing():
    old = ZendureManager.soc_balancing
    ZendureManager.soc_balancing = True
    yield
    ZendureManager.soc_balancing = old


@pytest.mark.parametrize("case", make_params(CASES))
async def test_soc_balancing_matches_spec(case: Case) -> None:
    devs = await drive_metered(ManagerMode[case.mode], case)

    assert_matches_spec(devs, case)


async def test_charge_balancing_full_device_gets_nothing() -> None:
    """A full battery takes no charge when balancing - in the weighted
    path its weight (100 - SoC) is 0, so this must match.
    """
    dev_full = _FakeDevice(electric_level=100, state=DeviceState.SOCFULL)
    dev_full.pwr_max = -1200
    dev_low = _FakeDevice(electric_level=50, state=DeviceState.INACTIVE)
    dev_low.pwr_max = -1200
    mgr = _manager(dev_low)
    mgr.charge = [dev_full, dev_low]
    mgr.charge_limit = -2400
    mgr.charge_optimal = 600
    mgr.charge_time = datetime.min  # skip the charge hysteria
    mgr.charge_last = datetime.min

    await mgr.power_charge(-500, datetime.now())

    assert dev_low.charge_calls == [-500]
    assert dev_full.charge_calls == [0]


async def test_discharge_balancing_full_device_without_solar_discharges() -> None:
    """A full battery with no solar passes nothing on its own, so it must
    take the discharge budget as the fullest battery. Zeroing it made the
    device go idle, get restarted and be zeroed again every other cycle.
    """
    dev_full = _FakeDevice(electric_level=100, state=DeviceState.SOCFULL)
    dev_low = _FakeDevice(electric_level=70, state=DeviceState.INACTIVE)
    mgr = _manager(dev_low)
    mgr.discharge = [dev_full, dev_low]
    mgr.discharge_limit = 2400
    mgr.discharge_optimal = 600
    mgr.discharge_weight = 1200 * 100 + 1200 * 70

    await mgr.power_discharge(300)

    assert dev_full.discharge_calls == [300]
    assert dev_low.discharge_calls == [0]

