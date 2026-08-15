"""MANUAL — manager-mode conformance against the spec CSV.

The device outputs/draws exactly the manual power (P1 ignored). 
Negative power grid-charges, so this mode is driven on an AC-capable device.
"""

from __future__ import annotations

import pytest

from custom_components.zendure_ha.const import ManagerMode

from .harness import Case, assert_matches_spec, drive_metered, load_cases_from_csv, make_params

CASES = load_cases_from_csv("manual")


@pytest.mark.parametrize("case", make_params(CASES))
async def test_manual_matches_spec(case: Case):
    devs = await drive_metered(ManagerMode.MANUAL, case)

    assert_matches_spec(devs, case)
