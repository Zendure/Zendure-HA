"""A paired ACE 1500 reports the Hub's battery packs — its capacity must not count twice.

The ACE 1500 bolts onto a Hub and reports the Hub's own AB-packs as its own: same serial
numbers, same SoC.
"""

from __future__ import annotations

import pytest

from custom_components.zendure_ha.const import DeviceState, SmartMode


def _wake(device) -> None:  # noqa: ANN001
    """Bring a device to a dispatchable baseline.

    Entities set `_attr_native_value` directly here: `update_value` on sensors is a no-op
    until the entity is attached to a platform, which these unit tests deliberately skip.
    """
    device.connectionStatus._attr_native_value = SmartMode.CONNECTED
    device.socSet._attr_native_value = 100
    device.minSoc._attr_native_value = 10
    device.electricLevel._attr_native_value = 50


@pytest.fixture
async def ace(hass):  # noqa: ANN001
    from custom_components.zendure_ha.devices.ace1500 import ACE1500

    device = ACE1500(hass, "ace-1500", "Ace 1500", {"productKey": "testProdKey", "snNumber": "ACETEST1", "productModel": "ACE 1500", "ip": ""})
    device.kWh = 3.84  # what packData reports: the Hub's 2x AB2000, seen a second time
    return device


async def test_paired_ace_reports_no_capacity(ace) -> None:  # noqa: ANN001
    assert ace.hubMode.value == 0, "paired is the default hubMode"
    assert ace.kWh == 0.0


async def test_standalone_ace_keeps_its_capacity(ace) -> None:  # noqa: ANN001
    """Standalone mode drives its own packs, so that capacity is real."""
    ace.hubMode.update_value(1)

    assert ace.kWh == pytest.approx(3.84)


async def test_paired_ace_stays_dispatchable(ace) -> None:  # noqa: ANN001
    """Zero capacity must not knock the ACE offline: Hub2000.charge() refuses AC charging,
    so a paired ACE is the only device that can pull from the grid. power_get() returning
    False would drop it out of the manager's distribution entirely."""
    ace.batteries = {"AB2000TEST": None}  # reports packs, even though they are the Hub's
    _wake(ace)

    assert ace.kWh == 0.0
    assert await ace.power_get() is True
    assert ace.state != DeviceState.OFFLINE


async def test_device_with_no_packs_still_goes_offline(ace) -> None:  # noqa: ANN001
    """The original guard still holds for a device that reports nothing at all."""
    ace.batteries = {}
    ace.kWh = 0.0
    _wake(ace)

    assert await ace.power_get() is False
    assert ace.state == DeviceState.OFFLINE
