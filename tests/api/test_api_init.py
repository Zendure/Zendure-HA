from __future__ import annotations

from pytest_mock import MockerFixture


class TestApiInit:

    def test_skips_cloud_mqtt_when_empty(self, mocker: MockerFixture) -> None:
        """Init with empty mqtt dict must not crash or call mqttInit."""
        from custom_components.zendure_ha.api import Api

        api = Api.__new__(Api)
        mock_init = mocker.patch.object(api, "mqttInit")

        api.Init({"mqttlog": False}, {})

        mock_init.assert_not_called()

    def test_connects_cloud_mqtt_when_clientid_present(self, mocker: MockerFixture) -> None:
        """Init calls mqttInit when mqtt dict contains clientId."""
        from custom_components.zendure_ha.api import Api

        api = Api.__new__(Api)
        mocker.patch.object(type(Api.mqttCloud), "__init__", return_value=None)
        mock_init = mocker.patch.object(api, "mqttInit")

        api.Init({"mqttlog": False}, {
            "clientId": "test-client",
            "url": "mqtt.zendure.tech:1883",
            "username": "user",
            "password": "pass",
        })

        mock_init.assert_called_once()
