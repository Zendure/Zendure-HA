from __future__ import annotations

import pytest
from pytest_mock import MockerFixture


def _make_zensdk_with_mqtt(mocker: MockerFixture) -> object:
    from custom_components.zendure_ha.device import ZendureZenSdk

    device = mocker.MagicMock(spec=ZendureZenSdk)
    device.connection = mocker.MagicMock()
    device.connection.value = 2
    device.mqtt = mocker.MagicMock()
    return device


class TestMqttDisconnectClearsDeviceMqtt:
    """After MQTT broker disconnect, device.mqtt must be None so dataRefresh falls back to HTTP."""

    def test_disconnect_clears_mqtt_on_affected_devices(self, mocker: MockerFixture) -> None:
        """mqttDisconnect with the matching client sets device.mqtt = None."""
        from custom_components.zendure_ha.api import Api

        device = _make_zensdk_with_mqtt(mocker)
        client = device.mqtt

        manager = mocker.MagicMock(spec=Api)
        manager.devices = {"dev1": device}
        manager.mqttDisconnect = Api.mqttDisconnect.__get__(manager, Api)

        manager.mqttDisconnect(client, "local", None, 0, None)

        assert device.mqtt is None, (
            "device.mqtt must be cleared on disconnect so dataRefresh resumes HTTP polling"
        )

    def test_disconnect_does_not_clear_unrelated_client(self, mocker: MockerFixture) -> None:
        """mqttDisconnect with a different client must not touch device.mqtt."""
        from custom_components.zendure_ha.api import Api

        device = _make_zensdk_with_mqtt(mocker)
        other_client = mocker.MagicMock()

        manager = mocker.MagicMock(spec=Api)
        manager.devices = {"dev1": device}
        manager.mqttDisconnect = Api.mqttDisconnect.__get__(manager, Api)

        manager.mqttDisconnect(other_client, "local", None, 0, None)

        assert device.mqtt is not None, (
            "device.mqtt must be unchanged when a different client disconnects"
        )

    @pytest.mark.asyncio
    async def test_data_refresh_polls_after_disconnect(self, mocker: MockerFixture) -> None:
        """After mqtt is cleared, dataRefresh must call httpGet instead of returning early."""
        import types
        from custom_components.zendure_ha.device import ZendureZenSdk

        device = mocker.MagicMock()
        device.connection = mocker.MagicMock()
        device.connection.value = 2
        device.mqtt = None  # cleared by disconnect
        device.online = True
        device.httpGet = mocker.AsyncMock(return_value={})
        device.mqttProperties = mocker.AsyncMock()
        device.dataRefresh = types.MethodType(ZendureZenSdk.dataRefresh, device)

        await device.dataRefresh(1)

        device.httpGet.assert_called_once_with("properties/report")
