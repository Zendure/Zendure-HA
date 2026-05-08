from __future__ import annotations

import pytest
from pytest_mock import MockerFixture

from tests.device.helpers import _make_zensdk_device


class TestDoCommandRouting:
    """Test that doCommand dispatches to the right channel based on connection mode."""

    @pytest.mark.asyncio
    async def test_zensdk_with_mqtt_calls_mqttpublish_on_topic_write(self, mocker: MockerFixture) -> None:
        """zenSDK mode (connection=2) with mqtt connected → mqttPublish on topic_write."""
        device = _make_zensdk_device(mocker, connection_value=2, has_mqtt=True)
        command = {"properties": {"outputLimit": 100}}
        await device.doCommand(command)

        device.mqttPublish.assert_called_once_with(device.topic_write, command, device.mqtt)
        device.httpPost.assert_not_called()

    @pytest.mark.asyncio
    async def test_zensdk_without_mqtt_calls_httppost(self, mocker: MockerFixture) -> None:
        """zenSDK mode (connection=2) with no mqtt → fall through to httpPost."""
        device = _make_zensdk_device(mocker, connection_value=2, has_mqtt=False)
        command = {"properties": {"outputLimit": 100}}
        await device.doCommand(command)

        device.httpPost.assert_called_once_with("properties/write", command)
        device.mqttPublish.assert_not_called()

    @pytest.mark.asyncio
    async def test_cloud_mode_calls_mqttpublish(self, mocker: MockerFixture) -> None:
        """Cloud mode (connection=0) → mqttPublish called, httpPost not called."""
        device = _make_zensdk_device(mocker, connection_value=0, has_mqtt=False)
        command = {"properties": {"outputLimit": 50}}
        await device.doCommand(command)

        device.mqttPublish.assert_called_once_with(device.topic_write, command, device.mqtt)
        device.httpPost.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_props_single_mqttpublish(self, mocker: MockerFixture) -> None:
        """zenSDK mode with two properties → single mqttPublish call with full command."""
        device = _make_zensdk_device(mocker, connection_value=2, has_mqtt=True)
        command = {"properties": {"outputLimit": 100, "inputLimit": 0}}
        await device.doCommand(command)

        device.mqttPublish.assert_called_once_with(device.topic_write, command, device.mqtt)
