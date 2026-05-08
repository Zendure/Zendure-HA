from __future__ import annotations

import pytest
from pytest_mock import MockerFixture

from tests.api.helpers import CLOUD_EMPTY, _mock_http_response, hass  # noqa: F401


class TestApiHAFallback:

    TOKEN = "aHR0cHM6Ly9hcHAuemVuZHVyZS50ZWNoL2V1LjQ4clFLRGFUOQ=="

    @pytest.mark.asyncio
    async def test_calls_local_discovery_when_cloud_empty(
        self, hass: object, mocker: MockerFixture
    ) -> None:
        """When cloud returns empty deviceList and device_ip set → LocalDiscovery called."""
        from custom_components.zendure_ha.api import Api
        from custom_components.zendure_ha.const import CONF_DEVICE_IP

        local_result = {
            "mqtt": {},
            "deviceList": [{"deviceKey": "EOD1NLN9P010318", "productModel": "solarFlow800Pro2",
                            "deviceName": "solarFlow800Pro2", "snNumber": "EOD1NLN9P010318",
                            "ip": "192.168.10.80"}],
        }

        import custom_components.zendure_ha.api as api_mod

        session = mocker.MagicMock()
        session.post = _mock_http_response(mocker, CLOUD_EMPTY)
        mocker.patch.object(api_mod, "async_get_clientsession", return_value=session)
        mock_local = mocker.patch.object(Api, "LocalDiscovery", new=mocker.AsyncMock(return_value=local_result))

        result = await Api.ApiHA(hass, {"token": self.TOKEN, CONF_DEVICE_IP: "192.168.10.80"})

        mock_local.assert_called_once_with(hass, "192.168.10.80")
        assert result is not None
        sns = {d["snNumber"] for d in result["deviceList"]}
        assert "EOD1NLN9P010318" in sns

    @pytest.mark.asyncio
    async def test_returns_none_without_device_ip(
        self, hass: object, mocker: MockerFixture
    ) -> None:
        """Empty cloud list + no device_ip → None."""
        from custom_components.zendure_ha.api import Api

        import custom_components.zendure_ha.api as api_mod

        session = mocker.MagicMock()
        session.post = _mock_http_response(mocker, CLOUD_EMPTY)
        mocker.patch.object(api_mod, "async_get_clientsession", return_value=session)
        mock_local = mocker.patch.object(Api, "LocalDiscovery", new=mocker.AsyncMock(return_value=None))

        result = await Api.ApiHA(hass, {"token": self.TOKEN})

        mock_local.assert_not_called()
        assert result is None
