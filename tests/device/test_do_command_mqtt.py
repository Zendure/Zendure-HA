from __future__ import annotations

import types

import pytest
from pytest_mock import MockerFixture, MockType


def _make_zensdk_device_with_mqtt(mocker: MockerFixture) -> MockType:
    """Build a ZendureZenSdk stub with a mocked MQTT client (connection=2)."""
    from custom_components.zendure_ha.device import ZendureZenSdk

    device = mocker.MagicMock()
    device.connection.value = 2
    device.deviceId = "EOD1NLN9P010318"
    device.topic_write = f"iot/testkey/{device.deviceId}/properties/write"
    device.mqtt = mocker.MagicMock()
    device.httpPost = mocker.AsyncMock()
    device.mqttPublish = mocker.MagicMock()
    device.doCommand = types.MethodType(ZendureZenSdk.doCommand, device)
    return device


class TestDoCommandZenSdkRouting:
    """Verify that doCommand uses httpPost for zenSDK devices (connection=2)."""

    @pytest.mark.asyncio
    async def test_acmode_1_calls_httppost(self, mocker: MockerFixture) -> None:
        """connection=2 → httpPost with acMode=1, not mqttPublish."""
        device = _make_zensdk_device_with_mqtt(mocker)

        await device.doCommand({"properties": {"acMode": 1}})

        device.httpPost.assert_called_once_with("properties/write", {"properties": {"acMode": 1}})
        device.mqttPublish.assert_not_called()

    @pytest.mark.asyncio
    async def test_acmode_2_calls_httppost(self, mocker: MockerFixture) -> None:
        """connection=2 → httpPost with acMode=2, not mqttPublish."""
        device = _make_zensdk_device_with_mqtt(mocker)

        await device.doCommand({"properties": {"acMode": 2}})

        device.httpPost.assert_called_once_with("properties/write", {"properties": {"acMode": 2}})
        device.mqttPublish.assert_not_called()

    @pytest.mark.asyncio
    async def test_cloud_mode_calls_mqttpublish(self, mocker: MockerFixture) -> None:
        """connection=0 (cloud) → mqttPublish on topic_write, not httpPost."""
        device = _make_zensdk_device_with_mqtt(mocker)
        device.connection.value = 0
        command = {"properties": {"acMode": 1}}

        await device.doCommand(command)

        device.mqttPublish.assert_called_once_with(device.topic_write, command, device.mqtt)
        device.httpPost.assert_not_called()
