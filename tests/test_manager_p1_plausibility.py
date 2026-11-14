"""Tests for the opt-in P1 plausibility check.

P1 plus what the devices feed into the home, minus what they draw from it, is the
house load. It cannot be clearly negative. A P1 meter that delivered its readings
minutes late made the smart modes swing between charging from and feeding into the
grid (e.g. charging 1000 W while P1 still showed -640 W of feed-in). With
p1_check_time > 0 the manager holds the devices at 0 W while P1 contradicts them.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.zendure_ha import manager as manager_module
from custom_components.zendure_ha.const import DeviceState, ManagerMode, ManagerState
from custom_components.zendure_ha.manager import P1_NOTIFICATION_ID, ZendureManager

NOW = datetime(2026, 11, 14, 13, 42, 0)


class _Sensor:
    def __init__(self) -> None:
        self.values: list = []

    def update_value(self, value) -> None:  # noqa: ANN001
        self.values.append(value)


def _device(*, home_output: int = 0, home_input: int = 0, bypass: int = 0) -> SimpleNamespace:
    """A device with just what powerChanged and the check read and call."""
    return SimpleNamespace(
        power_get=AsyncMock(return_value=True),
        power_charge=AsyncMock(return_value=0),
        power_discharge=AsyncMock(return_value=0),
        homeOutput=SimpleNamespace(asInt=home_output),
        homeInput=SimpleNamespace(asInt=home_input),
        batteryOutput=SimpleNamespace(asInt=home_output),
        batteryInput=SimpleNamespace(asInt=home_input),
        solarInput=SimpleNamespace(asInt=0),
        byPass=SimpleNamespace(asInt=bypass),
        electricLevel=SimpleNamespace(asInt=50, asNumber=50),
        state=DeviceState.INACTIVE,
        pwr_offgrid=0,
        pwr_produced=0,
        pwr_max=800,
        charge_optimal=0,
        discharge_optimal=0,
        exports_bypass=False,
        actualKwh=1.0,
        kWh=3.84,
        fuseGrp=SimpleNamespace(charge_limit=lambda _d: -1000, discharge_limit=lambda _d: 800),
    )


@pytest.fixture
def notifications(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    fake = SimpleNamespace(async_create=Mock(), async_dismiss=Mock())
    monkeypatch.setattr(manager_module, "persistent_notification", fake)
    return fake


def _manager(check_time: int, operation: ManagerMode = ManagerMode.MATCHING) -> ZendureManager:
    manager = object.__new__(ZendureManager)
    manager.hass = SimpleNamespace()
    manager.operation = operation
    manager.p1checktime = SimpleNamespace(asInt=check_time)
    manager.p1_implausible = False
    manager.p1_implausible_since = None
    manager.operationstate = _Sensor()
    manager.charge = []
    manager.discharge = []
    manager.idle = []
    return manager


@pytest.mark.usefixtures("notifications")
async def test_disabled_by_default() -> None:
    manager = _manager(check_time=0)
    manager.charge = [device := _device(home_input=1000)]

    assert await manager._p1_plausible(-1640, NOW)  # noqa: SLF001
    device.power_charge.assert_not_awaited()


@pytest.mark.usefixtures("notifications")
async def test_ignored_in_manual_mode() -> None:
    manager = _manager(check_time=20, operation=ManagerMode.MANUAL)
    manager.p1_implausible_since = NOW - timedelta(seconds=60)

    assert await manager._p1_plausible(-1640, NOW)  # noqa: SLF001
    assert manager.p1_implausible_since is None


@pytest.mark.usefixtures("notifications")
async def test_small_negative_house_load_is_tolerated() -> None:
    manager = _manager(check_time=20)

    assert await manager._p1_plausible(-100, NOW)  # noqa: SLF001
    assert manager.p1_implausible_since is None


async def test_short_contradictions_keep_regulating(notifications) -> None:  # noqa: ANN001
    manager = _manager(check_time=20)
    manager.charge = [device := _device(home_input=1000)]

    assert await manager._p1_plausible(-1640, NOW)  # noqa: SLF001
    assert await manager._p1_plausible(-1640, NOW + timedelta(seconds=19))  # noqa: SLF001

    assert manager.p1_implausible_since == NOW
    device.power_charge.assert_not_awaited()
    notifications.async_create.assert_not_called()


async def test_lasting_contradiction_holds_devices_at_zero(notifications) -> None:  # noqa: ANN001
    manager = _manager(check_time=20)
    charging = _device(home_input=1000)
    discharging = _device(home_output=800)
    bypassing = _device(home_output=300, bypass=1)
    manager.charge = [charging]
    manager.discharge = [discharging, bypassing]

    assert await manager._p1_plausible(-1640, NOW)  # noqa: SLF001
    assert not await manager._p1_plausible(-1640, NOW + timedelta(seconds=20))  # noqa: SLF001

    charging.power_charge.assert_awaited_once_with(0)
    discharging.power_discharge.assert_awaited_once_with(0)
    bypassing.power_discharge.assert_not_awaited()
    assert manager.operationstate.values == [ManagerState.P1_IMPLAUSIBLE.value]
    notifications.async_create.assert_called_once()
    assert notifications.async_create.call_args.args[3] == P1_NOTIFICATION_ID


async def test_notification_is_created_once_and_dismissed_on_recovery(notifications) -> None:  # noqa: ANN001
    manager = _manager(check_time=20)
    manager.p1_implausible_since = NOW - timedelta(seconds=30)

    assert not await manager._p1_plausible(-1640, NOW)  # noqa: SLF001
    assert not await manager._p1_plausible(-1500, NOW + timedelta(seconds=5))  # noqa: SLF001
    assert await manager._p1_plausible(150, NOW + timedelta(seconds=10))  # noqa: SLF001

    notifications.async_create.assert_called_once()
    notifications.async_dismiss.assert_called_once_with(manager.hass, P1_NOTIFICATION_ID)
    assert not manager.p1_implausible
    assert manager.p1_implausible_since is None


def _power_changed_manager(check_time: int, since: datetime | None) -> ZendureManager:
    manager = _manager(check_time)
    manager.p1_implausible_since = since
    manager.devices = [_device(home_input=1000)]  # charging 1000 W from the grid
    manager.power = _Sensor()
    manager.availableKwh = _Sensor()
    manager.globalSoc = _Sensor()
    for attr in ("charge_limit", "charge_optimal", "charge_weight", "discharge_bypass", "discharge_limit",
                 "discharge_optimal", "discharge_produced", "discharge_weight", "idle_lvlmax", "produced"):
        setattr(manager, attr, 0)
    manager.idle_lvlmin = 100
    manager.power_charge = AsyncMock()
    manager.power_discharge = AsyncMock()
    return manager


@pytest.mark.usefixtures("notifications")
async def test_power_changed_stops_regulating_on_stale_p1() -> None:
    # P1 still shows -640 W feed-in from minutes ago while the device charges 1000 W
    manager = _power_changed_manager(check_time=20, since=NOW - timedelta(seconds=30))

    await manager.powerChanged(-640, False, NOW)

    manager.power_charge.assert_not_awaited()
    manager.power_discharge.assert_not_awaited()
    manager.devices[0].power_charge.assert_awaited_once_with(0)


@pytest.mark.usefixtures("notifications")
async def test_power_changed_regulates_when_p1_is_plausible() -> None:
    # Same device, but P1 shows the real import: house = 1150 - 1000 = 150 W
    manager = _power_changed_manager(check_time=20, since=None)

    await manager.powerChanged(1150, False, NOW)

    assert manager.power_charge.await_count + manager.power_discharge.await_count == 1
    assert not manager.p1_implausible
