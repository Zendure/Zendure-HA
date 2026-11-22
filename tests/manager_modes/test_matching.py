"""MATCHING — manager-mode conformance against the spec CSV.

Balances P1 to zero using PV + battery; on a device (no AC-charge path) it
never grid-charges, so negative-P1 rows just store the device's own solar.
"""

from __future__ import annotations

import pytest

from custom_components.zendure_ha.const import ManagerMode

from .harness import Case, assert_matches_spec, drive_metered, load_cases_from_csv, make_params

CASES = load_cases_from_csv("matching")


@pytest.mark.parametrize("case", make_params(CASES))
async def test_matching_matches_spec(case: Case):
    devs = await drive_metered(ManagerMode.MATCHING, case)

    assert_matches_spec(devs, case)
