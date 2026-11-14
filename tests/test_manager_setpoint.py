"""Tests for applying user setpoint changes without waiting for a P1 update.

The power distribution normally runs only when the P1 sensor changes state. A
P1 sensor that reports rarely (or not at all while its value is unchanged)
delayed a new manual power or operation mode until its next update - observed
as 63-68 s with a P1 meter that sends about one value per minute. The manager
now remembers the last P1 value and re-runs the distribution right away.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.zendure_ha.const import ManagerMode
from custom_components.zendure_ha.manager import ZendureManager

P1 = 150


def _make_manager(operation: ManagerMode, p1_last: int | None = P1) -> ZendureManager:
    manager = object.__new__(ZendureManager)
    manager.hass = SimpleNamespace(is_running=True)
    manager.operation = operation
    manager.p1_last = p1_last
    manager.p1_factor = 1
    manager.p1_history = deque([25, -25], maxlen=8)
    manager.p1meterEvent = object()
    manager.devices = [SimpleNamespace(online=True, power_off=AsyncMock())]
    manager.fuseGroups = []
    manager.charge = []
    manager.discharge = []
    manager.idle = []
    manager.zero_fast = datetime.min
    manager.zero_next = datetime.min
    manager.powerChanged = AsyncMock()
    return manager


async def test_manual_power_change_is_applied_immediately_in_manual_mode() -> None:
    manager = _make_manager(ManagerMode.MANUAL)

    await manager.update_manual_power(None, 300)

    manager.powerChanged.assert_awaited_once()
    p1, is_fast, _time = manager.powerChanged.await_args.args
    assert (p1, is_fast) == (P1, True)


async def test_manual_power_change_does_nothing_outside_manual_mode() -> None:
    manager = _make_manager(ManagerMode.MATCHING)

    await manager.update_manual_power(None, 300)

    manager.powerChanged.assert_not_awaited()


async def test_nothing_is_applied_before_the_first_p1_value() -> None:
    # Also covers the restore at startup, which calls the onwrite callback.
    manager = _make_manager(ManagerMode.MANUAL, p1_last=None)

    await manager.update_manual_power(None, 300)

    manager.powerChanged.assert_not_awaited()


async def test_nothing_is_applied_while_a_distribution_round_is_running() -> None:
    manager = _make_manager(ManagerMode.MANUAL)
    manager.zero_fast = datetime.max  # set by _distribute while it runs

    await manager.update_manual_power(None, 300)

    manager.powerChanged.assert_not_awaited()


async def test_redistribution_releases_the_rate_limit_lock() -> None:
    manager = _make_manager(ManagerMode.MANUAL)

    await manager.update_manual_power(None, 300)

    now = datetime.now()
    assert now < manager.zero_fast < datetime.max
    assert now < manager.zero_next < datetime.max


async def test_switching_the_operation_mode_is_applied_immediately() -> None:
    manager = _make_manager(ManagerMode.OFF)

    await manager.update_operation(SimpleNamespace(value=ManagerMode.MATCHING.value), None)

    assert manager.operation == ManagerMode.MATCHING
    manager.powerChanged.assert_awaited_once()
    assert manager.powerChanged.await_args.args[0] == P1


async def test_switching_to_off_powers_off_without_a_distribution_round() -> None:
    manager = _make_manager(ManagerMode.MANUAL)

    await manager.update_operation(SimpleNamespace(value=ManagerMode.OFF.value), None)

    manager.devices[0].power_off.assert_awaited_once()
    manager.powerChanged.assert_not_awaited()


async def test_p1_update_remembers_the_last_value_even_when_rate_limited() -> None:
    manager = _make_manager(ManagerMode.MATCHING, p1_last=None)
    manager.zero_fast = datetime.max  # rate limited: the value is only buffered
    event = SimpleNamespace(data={"new_state": SimpleNamespace(state="123")})

    await manager._p1_changed(event)  # noqa: SLF001

    assert manager.p1_last == 123
    manager.powerChanged.assert_not_awaited()
