"""MATCHING_DISCHARGE — manager-mode conformance against the spec CSV.

`power_discharge(max(0, setpoint))` — discharge only, NEVER charge: covers the
demand; solar beyond the demand is stored. Solar pass-through is PASSTHROUGH_SOLAR.
"""

from __future__ import annotations

import pytest

from custom_components.zendure_ha.const import ManagerMode

from .harness import Case, assert_matches_spec, drive_metered, load_cases_from_csv, make_params

CASES = load_cases_from_csv("matching_discharge")


@pytest.mark.parametrize("case", make_params(CASES))
async def test_matching_discharge_matches_spec(case: Case):
    devs = await drive_metered(ManagerMode.MATCHING_DISCHARGE, case)

    assert_matches_spec(devs, case)
