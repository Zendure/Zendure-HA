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


class TestDoCommandMqttPublish:
    """Verify that doCommand routes to mqttPublish on topic_write when MQTT is active."""

    @pytest.mark.asyncio
    async def test_acmode_1_calls_mqttpublish_on_topic_write(self, mocker: MockerFixture) -> None:
        """connection=2 + mqtt set → mqttPublish called with topic_write and acMode=1."""
        device = _make_zensdk_device_with_mqtt(mocker)

        await device.doCommand({"properties": {"acMode": 1}})

        device.mqttPublish.assert_called_once_with(
            device.topic_write,
            {"properties": {"acMode": 1}},
            device.mqtt,
        )

    @pytest.mark.asyncio
    async def test_acmode_2_calls_mqttpublish_on_topic_write(self, mocker: MockerFixture) -> None:
        """connection=2 + mqtt set → mqttPublish called with topic_write and acMode=2."""
        device = _make_zensdk_device_with_mqtt(mocker)

        await device.doCommand({"properties": {"acMode": 2}})

        device.mqttPublish.assert_called_once_with(
            device.topic_write,
            {"properties": {"acMode": 2}},
            device.mqtt,
        )

    @pytest.mark.asyncio
    async def test_no_mqtt_falls_back_to_httppost(self, mocker: MockerFixture) -> None:
        """When mqtt is None → httpPost called instead of mqttPublish."""
        device = _make_zensdk_device_with_mqtt(mocker)
        device.mqtt = None

        await device.doCommand({"properties": {"acMode": 1}})

        device.httpPost.assert_called_once()
        device.mqttPublish.assert_not_called()
