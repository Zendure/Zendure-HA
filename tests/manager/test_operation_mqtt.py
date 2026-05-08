from __future__ import annotations

import types

import pytest
from pytest_mock import MockerFixture


def _make_manager_with_real_docommand(mocker: MockerFixture, *, zensdk_devices: int = 1) -> tuple:
    """Build a manager stub where each zenSDK device runs real doCommand with a mocked MQTT client."""
    from custom_components.zendure_ha.const import ManagerMode
    from custom_components.zendure_ha.device import ZendureZenSdk
    from custom_components.zendure_ha.manager import ZendureManager

    zen_devs = []
    for _ in range(zensdk_devices):
        # spec=ZendureZenSdk makes isinstance() return True (needed by update_operation).
        # connection is set explicitly because it's an instance attribute, not class-level.
        d = mocker.MagicMock(spec=ZendureZenSdk)
        d.connection = mocker.MagicMock()
        d.connection.value = 2
        d.deviceId = f"EOD1NLN9P01031{len(zen_devs)}"
        d.topic_write = f"iot/testkey/{d.deviceId}/properties/write"
        d.mqtt = mocker.MagicMock()
        d.httpPost = mocker.AsyncMock()
        d.mqttPublish = mocker.MagicMock()
        d.doCommand = types.MethodType(ZendureZenSdk.doCommand, d)
        d.online = True
        zen_devs.append(d)

    manager = mocker.MagicMock(spec=ZendureManager)
    manager.operation = ManagerMode.OFF
    manager.p1meterEvent = mocker.MagicMock()
    manager.devices = zen_devs
    manager.update_operation = types.MethodType(ZendureManager.update_operation, manager)
    return manager, zen_devs


def _entity(mocker: MockerFixture, value: int) -> object:
    e = mocker.MagicMock()
    e.value = value
    return e


class TestUpdateOperationMqttPublish:
    """Verify update_operation triggers mqttPublish on topic_write end-to-end."""

    @pytest.mark.asyncio
    async def test_smart_charging_publishes_acmode_input(self, mocker: MockerFixture) -> None:
        """smart_charging (4) → mqttPublish on topic_write with acMode=1."""
        manager, zen_devs = _make_manager_with_real_docommand(mocker)

        await manager.update_operation(_entity(mocker, 4), None)

        zen_devs[0].mqttPublish.assert_called_once_with(
            zen_devs[0].topic_write,
            {"properties": {"acMode": 1}},
            zen_devs[0].mqtt,
        )

    @pytest.mark.asyncio
    async def test_smart_discharging_publishes_acmode_output(self, mocker: MockerFixture) -> None:
        """smart_discharging (3) → mqttPublish on topic_write with acMode=2."""
        manager, zen_devs = _make_manager_with_real_docommand(mocker)

        await manager.update_operation(_entity(mocker, 3), None)

        zen_devs[0].mqttPublish.assert_called_once_with(
            zen_devs[0].topic_write,
            {"properties": {"acMode": 2}},
            zen_devs[0].mqtt,
        )

    @pytest.mark.asyncio
    async def test_smart_mode_does_not_publish_acmode(self, mocker: MockerFixture) -> None:
        """smart (2) → no mqttPublish for acMode."""
        manager, zen_devs = _make_manager_with_real_docommand(mocker)

        await manager.update_operation(_entity(mocker, 2), None)

        zen_devs[0].mqttPublish.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_devices_all_publish_acmode(self, mocker: MockerFixture) -> None:
        """All zenSDK devices publish acMode on topic_write — not just the first."""
        manager, zen_devs = _make_manager_with_real_docommand(mocker, zensdk_devices=3)

        await manager.update_operation(_entity(mocker, 4), None)

        for d in zen_devs:
            d.mqttPublish.assert_called_once_with(
                d.topic_write,
                {"properties": {"acMode": 1}},
                d.mqtt,
            )
