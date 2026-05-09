from __future__ import annotations

import pytest
from pytest_mock import MockerFixture

from tests.api.helpers import _mock_http_response, hass  # noqa: F401

CLOUD_EMPTY_WITH_MQTT = {
    "code": 200,
    "success": True,
    "data": {
        "mqtt": {"clientId": "c1", "url": "mqtt.zendure.tech:1883", "username": "u", "password": "p"},
        "deviceList": [],
    },
    "msg": "Operation successful",
}

LOCAL_SF_PRO2 = {
    "mqtt": {},
    "deviceList": [
        {"snNumber": "EOD1NLN9P010318", "productModel": "solarFlow800Pro2", "ip": "192.168.10.80"},
    ],
}

TOKEN = "aHR0cHM6Ly9hcHAuemVuZHVyZS50ZWNoL2V1LjQ4clFLRGFUOQ=="


class TestEmptyListFallback:
    """Empty cloud deviceList + device_ip must merge local device, not discard cloud MQTT."""

    @pytest.mark.asyncio
    async def test_empty_cloud_list_merges_local_device(
        self, hass: object, mocker: MockerFixture
    ) -> None:
        """Cloud returns empty list → local device is added, cloud MQTT credentials preserved."""
        import custom_components.zendure_ha.api as api_mod
        from custom_components.zendure_ha.api import Api
        from custom_components.zendure_ha.const import CONF_DEVICE_IP

        session = mocker.MagicMock()
        session.post = _mock_http_response(mocker, CLOUD_EMPTY_WITH_MQTT)
        mocker.patch.object(api_mod, "async_get_clientsession", return_value=session)
        mocker.patch.object(Api, "LocalDiscovery", new=mocker.AsyncMock(return_value=LOCAL_SF_PRO2))

        result = await Api.ApiHA(hass, {"token": TOKEN, CONF_DEVICE_IP: "192.168.10.80"})

        assert result is not None
        # Local device must be present
        sns = {d["snNumber"] for d in result["deviceList"]}
        assert "EOD1NLN9P010318" in sns, "zenSDK device must be merged from local discovery"
        # Cloud MQTT credentials must be preserved
        assert result.get("mqtt", {}).get("clientId") == "c1", (
            "Cloud MQTT credentials must not be discarded on empty deviceList"
        )

    @pytest.mark.asyncio
    async def test_empty_cloud_list_without_device_ip_returns_none(
        self, hass: object, mocker: MockerFixture
    ) -> None:
        """Cloud returns empty list and no device_ip → return None (no devices at all)."""
        import custom_components.zendure_ha.api as api_mod
        from custom_components.zendure_ha.api import Api

        session = mocker.MagicMock()
        session.post = _mock_http_response(mocker, CLOUD_EMPTY_WITH_MQTT)
        mocker.patch.object(api_mod, "async_get_clientsession", return_value=session)
        mock_local = mocker.patch.object(Api, "LocalDiscovery", new=mocker.AsyncMock(return_value=None))

        result = await Api.ApiHA(hass, {"token": TOKEN})

        mock_local.assert_not_called()
        assert result is None
