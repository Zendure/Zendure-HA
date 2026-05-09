from __future__ import annotations

import pytest
from pytest_mock import MockerFixture

from tests.api.helpers import CLOUD_WITH_HYPER, _mock_http_response, hass  # noqa: F401


class TestApiHAMixedScenario:
    """Cloud has legacy devices + device_ip points to a zenSDK device → both in result."""

    TOKEN = "aHR0cHM6Ly9hcHAuemVuZHVyZS50ZWNoL2V1LjQ4clFLRGFUOQ=="

    LOCAL_SF_PRO2 = {
        "mqtt": {},
        "deviceList": [
            {"deviceKey": "EOD1NLN9P010318", "productModel": "solarFlow800Pro2",
             "deviceName": "solarFlow800Pro2", "snNumber": "EOD1NLN9P010318", "ip": "192.168.10.80"},
        ],
    }

    @pytest.mark.asyncio
    async def test_zensdk_device_merged_into_cloud_list(
        self, hass: object, mocker: MockerFixture
    ) -> None:
        """Cloud returns Hyper 2000 + device_ip set → SF800Pro2 appended, total 2 devices."""
        import custom_components.zendure_ha.api as api_mod
        from custom_components.zendure_ha.api import Api
        from custom_components.zendure_ha.const import CONF_DEVICE_IP

        session = mocker.MagicMock()
        session.post = _mock_http_response(mocker, CLOUD_WITH_HYPER)
        mocker.patch.object(api_mod, "async_get_clientsession", return_value=session)
        mocker.patch.object(Api, "LocalDiscovery", new=mocker.AsyncMock(return_value=self.LOCAL_SF_PRO2))

        result = await Api.ApiHA(hass, {"token": self.TOKEN, CONF_DEVICE_IP: "192.168.10.80"})

        assert result is not None
        sns = {d["snNumber"] for d in result["deviceList"]}
        assert "HYP001" in sns
        assert "EOD1NLN9P010318" in sns
        assert len(result["deviceList"]) == 2

    @pytest.mark.asyncio
    async def test_duplicate_sn_not_added_twice(
        self, hass: object, mocker: MockerFixture
    ) -> None:
        """If zenSDK device SN already in cloud list, it must not be duplicated."""
        import custom_components.zendure_ha.api as api_mod
        from custom_components.zendure_ha.api import Api
        from custom_components.zendure_ha.const import CONF_DEVICE_IP

        cloud_with_same_sn = {
            "code": 200,
            "success": True,
            "data": {
                "mqtt": {"clientId": "c1", "url": "mqtt.zendure.tech:1883", "username": "u", "password": "p"},
                "deviceList": [
                    {"deviceKey": "EOD1NLN9P010318", "productModel": "solarFlow800Pro2",
                     "deviceName": "solarFlow800Pro2", "snNumber": "EOD1NLN9P010318", "ip": ""},
                ],
            },
            "msg": "Operation successful",
        }

        session = mocker.MagicMock()
        session.post = _mock_http_response(mocker, cloud_with_same_sn)
        mocker.patch.object(api_mod, "async_get_clientsession", return_value=session)
        mocker.patch.object(Api, "LocalDiscovery", new=mocker.AsyncMock(return_value=self.LOCAL_SF_PRO2))

        result = await Api.ApiHA(hass, {"token": self.TOKEN, CONF_DEVICE_IP: "192.168.10.80"})

        assert result is not None
        assert len(result["deviceList"]) == 1

    @pytest.mark.asyncio
    async def test_cloud_only_without_device_ip(
        self, hass: object, mocker: MockerFixture
    ) -> None:
        """Token set, no device_ip → cloud result returned as-is, LocalDiscovery not called."""
        import custom_components.zendure_ha.api as api_mod
        from custom_components.zendure_ha.api import Api

        session = mocker.MagicMock()
        session.post = _mock_http_response(mocker, CLOUD_WITH_HYPER)
        mocker.patch.object(api_mod, "async_get_clientsession", return_value=session)
        mock_local = mocker.patch.object(Api, "LocalDiscovery", new=mocker.AsyncMock())

        result = await Api.ApiHA(hass, {"token": self.TOKEN})

        mock_local.assert_not_called()
        assert result is not None
        assert result["deviceList"][0]["snNumber"] == "HYP001"

    @pytest.mark.asyncio
    async def test_token_free_with_device_ip(
        self, hass: object, mocker: MockerFixture
    ) -> None:
        """No token + device_ip → LocalDiscovery only, no cloud call."""
        import custom_components.zendure_ha.api as api_mod
        from custom_components.zendure_ha.api import Api
        from custom_components.zendure_ha.const import CONF_DEVICE_IP

        mocker.patch.object(api_mod, "async_get_clientsession")
        mock_local = mocker.patch.object(Api, "LocalDiscovery", new=mocker.AsyncMock(return_value=self.LOCAL_SF_PRO2))

        result = await Api.ApiHA(hass, {CONF_DEVICE_IP: "192.168.10.80"})

        mock_local.assert_called_once_with(hass, "192.168.10.80")
        assert result == self.LOCAL_SF_PRO2
        api_mod.async_get_clientsession.assert_not_called()
