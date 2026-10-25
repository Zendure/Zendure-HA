"""Tests for fuse group selection and the power limits it imposes.

Drives the real ZendureManager.update_fusegroups mapping (select option ->
FuseGroup limits) against a real device, then checks the limits FuseGroup hands
back and that ZendureManager.power_discharge never exceeds them.
"""

from __future__ import annotations

from custom_components.zendure_ha.const import ManagerMode
from custom_components.zendure_ha.manager import ZendureManager


def _make_manager_harness(devices: list) -> ZendureManager:
    manager = object.__new__(ZendureManager)
    manager.devices = devices
    manager.fuseGroups = []
    manager.operation = ManagerMode.OFF
    return manager


async def _select_fusegroup(manager: ZendureManager, device, option: str) -> None:  # noqa: ANN001
    device.fuseGroup._attr_current_option = option
    await manager.update_fusegroups()


async def test_group600_1200_is_a_selectable_option(zensdk_device) -> None:  # noqa: ANN001
    assert "group600_1200" in zensdk_device.fuseGroup.options


async def test_group600_1200_limits_device_to_600_out_1200_in(zensdk_device) -> None:  # noqa: ANN001
    # a device that can do 2400w each way on its own
    zensdk_device.setLimits(-2400, 2400)
    manager = _make_manager_harness([zensdk_device])

    await _select_fusegroup(manager, zensdk_device, "group600_1200")

    fg = zensdk_device.fuseGrp
    assert (fg.maxpower, fg.minpower) == (600, -1200)
    assert fg.discharge_limit(zensdk_device) == 600
    fg.initPower = True
    assert fg.charge_limit(zensdk_device) == -1200


async def test_group600_1200_caps_discharge_dispatch_at_600(zensdk_device) -> None:  # noqa: ANN001
    from tests.test_manager_dispatch import _make_manager_harness as _make_dispatch_harness

    zensdk_device.setLimits(-2400, 2400)
    await _select_fusegroup(_make_manager_harness([zensdk_device]), zensdk_device, "group600_1200")
    discharge_calls: list[int] = []

    async def _record(power: int) -> int:
        discharge_calls.append(power)
        return power

    zensdk_device.power_discharge = _record
    zensdk_device.electricLevel.update_value(80)

    manager = _make_dispatch_harness()
    manager.discharge = [zensdk_device]
    manager.discharge_limit = zensdk_device.fuseGrp.discharge_limit(zensdk_device)
    manager.discharge_weight = zensdk_device.pwr_max * zensdk_device.electricLevel.asInt

    await manager.power_discharge(1500)

    assert discharge_calls == [600]
