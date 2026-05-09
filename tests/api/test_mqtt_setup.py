from __future__ import annotations

import pytest
from pytest_mock import MockerFixture

from tests.api.helpers import _mock_http_response, hass  # noqa: F401


class TestZenSdkMqttSetup:
    """Tests for the HA.Mqtt.SetConfig RPC call."""

    @pytest.mark.asyncio
    async def test_success_returns_true(self, hass: object, mocker: MockerFixture) -> None:
        """Device responds without error field → True."""
        import custom_components.zendure_ha.api as api_mod
        from custom_components.zendure_ha.api import Api

        session = mocker.MagicMock()
        session.post = _mock_http_response(mocker, {"result": "ok"})
        mocker.patch.object(api_mod, "async_get_clientsession", return_value=session)

        result = await Api.ZenSdkMqttSetup(hass, "192.168.10.80", "SN001",
                                            "192.168.1.10", 1883, "user", "pass")
        assert result is True

    @pytest.mark.asyncio
    async def test_error_response_returns_false(self, hass: object, mocker: MockerFixture) -> None:
        """Device responds with error field → False."""
        import custom_components.zendure_ha.api as api_mod
        from custom_components.zendure_ha.api import Api

        session = mocker.MagicMock()
        session.post = _mock_http_response(mocker, {"error": {"code": -1, "message": "unknown method"}})
        mocker.patch.object(api_mod, "async_get_clientsession", return_value=session)

        result = await Api.ZenSdkMqttSetup(hass, "192.168.10.80", "SN001",
                                            "192.168.1.10", 1883, "user", "pass")
        assert result is False

    @pytest.mark.asyncio
    async def test_network_error_returns_false(self, hass: object, mocker: MockerFixture) -> None:
        """Network exception → False, no propagation."""
        import custom_components.zendure_ha.api as api_mod
        from custom_components.zendure_ha.api import Api

        session = mocker.MagicMock()
        session.post = mocker.MagicMock(side_effect=OSError("unreachable"))
        mocker.patch.object(api_mod, "async_get_clientsession", return_value=session)

        result = await Api.ZenSdkMqttSetup(hass, "192.168.10.80", "SN001",
                                            "192.168.1.10", 1883, "user", "pass")
        assert result is False

    @pytest.mark.asyncio
    async def test_correct_payload_sent(self, hass: object, mocker: MockerFixture) -> None:
        """Verify the RPC payload contains the correct method and config keys."""
        import custom_components.zendure_ha.api as api_mod
        from custom_components.zendure_ha.api import Api

        session = mocker.MagicMock()
        session.post = _mock_http_response(mocker, {"result": "ok"})
        mocker.patch.object(api_mod, "async_get_clientsession", return_value=session)

        await Api.ZenSdkMqttSetup(hass, "192.168.10.80", "SN001",
                                   "192.168.1.10", 1883, "mqttuser", "mqttpass")

        call_kwargs = session.post.call_args
        sent_json = call_kwargs[1]["json"]
        assert sent_json["method"] == "HA.Mqtt.SetConfig"
        assert sent_json["sn"] == "SN001"
        cfg = sent_json["params"]["config"]
        assert cfg["server"] == "192.168.1.10"
        assert cfg["port"] == 1883
        assert cfg["username"] == "mqttuser"
        assert cfg["password"] == "mqttpass"
        assert cfg["enable"] is True
