"""Tests for ZendureNumber.async_set_native_value clamping (PR #1506).

A device can narrow a number entity's usable range at runtime (e.g. a
SolarFlow 2400 Pro sporadically reporting inverseMaxPower=800 instead of
2400, see #1505). HA core only rejects writes above the entity's advertised
`native_max_value`, which we deliberately keep wide (see update_range's
"only ever widen" comment) so a set_value service call isn't rejected
before the integration ever sees it. Instead, async_set_native_value clamps
the outgoing value to the device's *current* [device_min, device_max] so
control degrades gracefully instead of being silently discarded.
"""

from __future__ import annotations

from homeassistant.components.number import NumberMode

from custom_components.zendure_ha.number import ZendureNumber


def _make_number(device, onwrite, maximum: int = 2400, minimum: int = 0) -> ZendureNumber:  # noqa: ANN001
    return ZendureNumber(device, "testLimit", onwrite, None, "W", "power", maximum, minimum, NumberMode.SLIDER)


async def test_set_value_within_range_passes_through_unclamped(zensdk_device) -> None:  # noqa: ANN001
    calls = []
    number = _make_number(zensdk_device, lambda _entity, value: calls.append(value))

    await number.async_set_native_value(1500)

    assert calls == [1500]


async def test_set_value_above_narrowed_device_max_is_clamped(zensdk_device) -> None:  # noqa: ANN001
    calls = []
    number = _make_number(zensdk_device, lambda _entity, value: calls.append(value))
    number.update_range(0, 800)  # device narrowed its usable range at runtime

    await number.async_set_native_value(2400)

    assert calls == [800]
    # The HA-facing max must stay wide so number.set_value(2400) isn't rejected
    # by core's own range validation before the integration sees it.
    assert number._attr_native_max_value == 2400  # noqa: SLF001


async def test_set_value_below_device_min_is_clamped(zensdk_device) -> None:  # noqa: ANN001
    calls = []
    number = _make_number(zensdk_device, lambda _entity, value: calls.append(value), minimum=100)

    await number.async_set_native_value(0)

    assert calls == [100]


async def test_update_range_never_narrows_advertised_max(zensdk_device) -> None:  # noqa: ANN001
    number = _make_number(zensdk_device, lambda _entity, _value: None)

    number.update_range(0, 800)

    assert number._attr_native_max_value == 2400  # noqa: SLF001
    assert number.device_max == 800


async def test_async_onwrite_is_awaited(zensdk_device) -> None:  # noqa: ANN001
    calls = []

    async def onwrite(_entity, value):
        calls.append(value)

    number = _make_number(zensdk_device, onwrite)

    await number.async_set_native_value(500)

    assert calls == [500]
