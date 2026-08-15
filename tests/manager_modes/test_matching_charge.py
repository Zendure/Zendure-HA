"""MATCHING_CHARGE — manager-mode conformance against the spec CSV.

Passes solar to home up to P1, stores the surplus, never discharges the battery;
on a device it never grid-charges
"""

from __future__ import annotations

import pytest

from custom_components.zendure_ha.const import ManagerMode

from .harness import Case, assert_matches_spec, drive_metered, load_cases_from_csv, make_params

CASES = load_cases_from_csv("matching_charge")


@pytest.mark.parametrize("case", make_params(CASES))
async def test_matching_charge_matches_spec(case: Case):
    devs = await drive_metered(ManagerMode.MATCHING_CHARGE, case)

    assert_matches_spec(devs, case)
