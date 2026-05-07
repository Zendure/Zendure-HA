"""Tests for local zenSDK discovery and productModel mapping.

Run with:
    uv run pytest tests/ -v
"""

from __future__ import annotations

import pytest
from pytest_mock import MockerFixture

# ── shared test data ─────────────────────────────────────────────────────────

PROPERTIES_REPORT = {
    "timestamp": 1778150268,
    "sn": "EOD1NLN9P010318",
    "version": 2,
    "product": "solarFlow800Pro2",
    "properties": {
        "electricLevel": 71,
        "outputPackPower": 285,
        "packInputPower": 0,
        "gridInputPower": 285,
        "solarInputPower": 0,
        "inverseMaxPower": 800,
        "acMode": 1,
        "smartMode": 1,
    },
}

CLOUD_EMPTY = {
    "code": 200,
    "success": True,
    "data": {"mqtt": {}, "deviceList": []},
    "msg": "Operation successful",
}


def _mock_http_response(mocker: MockerFixture, payload: dict) -> object:
    """Async mock for `await session.get/post(...)` — returns response directly."""
    resp = mocker.MagicMock()
    resp.json = mocker.AsyncMock(return_value=payload)
    resp.status = 200
    return mocker.AsyncMock(return_value=resp)


@pytest.fixture()
def hass(mocker: MockerFixture) -> object:
    return mocker.MagicMock()


# ── LocalDiscovery ───────────────────────────────────────────────────────────

class TestLocalDiscovery:

    @pytest.mark.asyncio
    async def test_happy_path(self, hass: object, mocker: MockerFixture) -> None:
        """Device at IP responds → returns synthesized deviceList entry."""
        import custom_components.zendure_ha.api as api_mod
        from custom_components.zendure_ha.api import Api

        session = mocker.MagicMock()
        session.get = _mock_http_response(mocker, PROPERTIES_REPORT)
        mocker.patch.object(api_mod, "async_get_clientsession", return_value=session)

        result = await Api.LocalDiscovery(hass, "192.168.10.80")

        assert result is not None
        assert result["mqtt"] == {}
        dev = result["deviceList"][0]
        assert dev["deviceKey"] == "EOD1NLN9P010318"
        assert dev["productModel"] == "solarFlow800Pro2"
        assert dev["snNumber"] == "EOD1NLN9P010318"
        assert dev["ip"] == "192.168.10.80"

    @pytest.mark.asyncio
    async def test_missing_fields_returns_none(self, hass: object, mocker: MockerFixture) -> None:
        """Response without sn/product → None."""
        import custom_components.zendure_ha.api as api_mod
        from custom_components.zendure_ha.api import Api

        session = mocker.MagicMock()
        session.get = _mock_http_response(mocker, {"timestamp": 1})
        mocker.patch.object(api_mod, "async_get_clientsession", return_value=session)

        assert await Api.LocalDiscovery(hass, "192.168.10.80") is None

    @pytest.mark.asyncio
    async def test_network_error_returns_none(self, hass: object, mocker: MockerFixture) -> None:
        """Device unreachable → None, no exception propagated."""
        import custom_components.zendure_ha.api as api_mod
        from custom_components.zendure_ha.api import Api

        session = mocker.MagicMock()
        session.get = mocker.MagicMock(side_effect=OSError("connection refused"))
        mocker.patch.object(api_mod, "async_get_clientsession", return_value=session)

        assert await Api.LocalDiscovery(hass, "192.168.10.80") is None


# ── productModel mapping ─────────────────────────────────────────────────────

class TestCreateDeviceMapping:

    def test_solarflow800pro2_mapped(self) -> None:
        """'solarflow800pro2' (lowercase) maps to SolarFlow800Pro2."""
        from custom_components.zendure_ha.api import Api
        from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro2

        assert Api.createdevice.get("solarflow800pro2") is SolarFlow800Pro2

    def test_manager_lookup_is_case_insensitive(self) -> None:
        """manager.py does .lower().strip() — camelCase product name from device must resolve."""
        from custom_components.zendure_ha.api import Api
        from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro2

        assert Api.createdevice.get("solarflow800pro2".lower().strip()) is SolarFlow800Pro2

    @pytest.mark.parametrize("key", [
        "hub 1200", "hub 2000", "hyper 2000", "solarflow 800", "solarflow 800 pro",
    ])
    def test_existing_models_not_removed(self, key: str) -> None:
        """Regression: existing model keys still present after our patch."""
        from custom_components.zendure_ha.api import Api

        assert key in Api.createdevice


# ── Api.Init empty-mqtt guard ─────────────────────────────────────────────────

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


# ── ApiHA → LocalDiscovery fallback ──────────────────────────────────────────

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
        assert result == local_result

    @pytest.mark.asyncio
    async def test_returns_none_without_device_ip(
        self, hass: object, mocker: MockerFixture
    ) -> None:
        """Empty cloud list + no device_ip → None."""
        from custom_components.zendure_ha.api import Api

        session = mocker.MagicMock()
        session.post = _mock_http_response(mocker, CLOUD_EMPTY)
        mocker.patch("custom_components.zendure_ha.api.async_get_clientsession", return_value=session)

        result = await Api.ApiHA(hass, {"token": self.TOKEN})

        assert result is None
