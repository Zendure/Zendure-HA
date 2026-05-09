from __future__ import annotations

import types

import pytest
from pytest_mock import MockerFixture


def _make_manager_stub(mocker: MockerFixture, *, p1meter: bool, zensdk_devices: int, legacy_devices: int = 0) -> object:
    """Build a minimal ZendureManager stub for update_operation tests."""
    from custom_components.zendure_ha.const import ManagerMode
    from custom_components.zendure_ha.device import ZendureLegacy, ZendureZenSdk
    from custom_components.zendure_ha.manager import ZendureManager

    manager = mocker.MagicMock(spec=ZendureManager)
    manager.operation = ManagerMode.OFF
    manager.p1meterEvent = mocker.MagicMock() if p1meter else None

    zen_devs = []
    for _ in range(zensdk_devices):
        d = mocker.MagicMock(spec=ZendureZenSdk)
        d.doCommand = mocker.AsyncMock()
        d.online = True
        zen_devs.append(d)

    leg_devs = []
    for _ in range(legacy_devices):
        d = mocker.MagicMock(spec=ZendureLegacy)
        d.power_off = mocker.AsyncMock()
        d.online = True
        leg_devs.append(d)

    manager.devices = zen_devs + leg_devs
    manager.update_operation = types.MethodType(ZendureManager.update_operation, manager)
    return manager, zen_devs, leg_devs


def _make_select_entity(mocker: MockerFixture, option_value: int) -> object:
    """Build a minimal select entity stub with .value property."""
    entity = mocker.MagicMock()
    entity.value = option_value
    return entity


class TestUpdateOperationAcMode:

    @pytest.mark.asyncio
    async def test_smart_charging_sets_acmode_input_on_zensdk(self, mocker: MockerFixture) -> None:
        """Switching to smart_charging (4) → acMode 1 sent to all zenSDK devices."""
        manager, zen_devs, _ = _make_manager_stub(mocker, p1meter=True, zensdk_devices=1)
        entity = _make_select_entity(mocker, 4)

        await manager.update_operation(entity, None)

        zen_devs[0].doCommand.assert_called_once_with({"properties": {"acMode": 1}})

    @pytest.mark.asyncio
    async def test_smart_discharging_sets_acmode_output_on_zensdk(self, mocker: MockerFixture) -> None:
        """Switching to smart_discharging (3) → acMode 2 sent to all zenSDK devices."""
        manager, zen_devs, _ = _make_manager_stub(mocker, p1meter=True, zensdk_devices=1)
        entity = _make_select_entity(mocker, 3)

        await manager.update_operation(entity, None)

        zen_devs[0].doCommand.assert_called_once_with({"properties": {"acMode": 2}})

    @pytest.mark.asyncio
    async def test_smart_mode_does_not_set_acmode(self, mocker: MockerFixture) -> None:
        """Switching to smart (2) → no acMode command sent."""
        manager, zen_devs, _ = _make_manager_stub(mocker, p1meter=True, zensdk_devices=1)
        entity = _make_select_entity(mocker, 2)

        await manager.update_operation(entity, None)

        zen_devs[0].doCommand.assert_not_called()

    @pytest.mark.asyncio
    async def test_acmode_sent_without_p1meter(self, mocker: MockerFixture) -> None:
        """acMode must be sent even when no P1 meter is configured."""
        manager, zen_devs, _ = _make_manager_stub(mocker, p1meter=False, zensdk_devices=1)
        entity = _make_select_entity(mocker, 4)

        await manager.update_operation(entity, None)

        zen_devs[0].doCommand.assert_called_once_with({"properties": {"acMode": 1}})

    @pytest.mark.asyncio
    async def test_legacy_device_does_not_get_acmode_command(self, mocker: MockerFixture) -> None:
        """Legacy devices must NOT receive doCommand for acMode."""
        manager, _, leg_devs = _make_manager_stub(mocker, p1meter=True, zensdk_devices=0, legacy_devices=1)
        entity = _make_select_entity(mocker, 4)

        await manager.update_operation(entity, None)

        assert not hasattr(leg_devs[0], "doCommand") or not leg_devs[0].doCommand.called

    @pytest.mark.asyncio
    async def test_multiple_zensdk_devices_all_get_acmode(self, mocker: MockerFixture) -> None:
        """All zenSDK devices receive the acMode command, not just the first."""
        manager, zen_devs, _ = _make_manager_stub(mocker, p1meter=True, zensdk_devices=3)
        entity = _make_select_entity(mocker, 4)

        await manager.update_operation(entity, None)

        for d in zen_devs:
            d.doCommand.assert_called_once_with({"properties": {"acMode": 1}})

