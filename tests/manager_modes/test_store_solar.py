"""STORE_SOLAR — manager-mode conformance against the spec CSV.

Sends all solar to the battery (never to home while P1 > 0). FULL bypasses.
On a device it never grid-charges.
"""

from __future__ import annotations

import pytest

from custom_components.zendure_ha.const import ManagerMode

from .harness import Case, assert_matches_spec, drive_metered, load_cases_from_csv, make_params

CASES = load_cases_from_csv("store_solar")


@pytest.mark.parametrize("case", make_params(CASES))
async def test_store_solar_matches_spec(case: Case):
    devs = await drive_metered(ManagerMode.STORE_SOLAR, case)

    assert_matches_spec(devs, case)
