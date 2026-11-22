"""PASSTHROUGH_SOLAR — manager-mode conformance against the spec CSV.

`power_discharge(max(self.produced - self.discharge_bypass, max(0, p1)))` — discharge
only, NEVER charge: every device passes its own solar to home (surplus exported, not
stored) and the battery only covers the gap when demand exceeds PV.
"""

from __future__ import annotations

import pytest

from custom_components.zendure_ha.const import ManagerMode

from .harness import Case, assert_matches_spec, drive_metered, load_cases_from_csv, make_params

CASES = load_cases_from_csv("passthrough_solar")


@pytest.mark.parametrize("case", make_params(CASES))
async def test_passthrough_solar_matches_spec(case: Case):
    devs = await drive_metered(ManagerMode.PASSTHROUGH_SOLAR, case)

    assert_matches_spec(devs, case)
