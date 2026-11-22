"""OFF — the manager performs no power distribution.

In OFF mode `powerChanged` only sets the operation state.
It must not command the device.
We assert the state is reported OFF and no discharge/charge command is issued, for any input.
"""

from __future__ import annotations

import pytest

from custom_components.zendure_ha.const import DeviceState, ManagerMode, ManagerState

from .harness import FakeDevice, build_manager, run_step

SOC_STATES = [DeviceState.SOCEMPTY, DeviceState.SOCFULL, DeviceState.INACTIVE]


@pytest.mark.parametrize("state", SOC_STATES, ids=[s.name for s in SOC_STATES])
@pytest.mark.parametrize("p1,pv", [(0, 0), (300, 200), (-100, 200)])
async def test_off_issues_no_commands(state: DeviceState, p1: int, pv: int):
    dev = FakeDevice(state, 50, pv=pv)
    dev.seed_spec(0, 0, 0)
    mgr = build_manager(ManagerMode.OFF, [dev])

    await run_step(mgr, p1)

    assert mgr.operationstate.value == ManagerState.OFF.value
    assert dev.commands == []  # OFF never drives the device
