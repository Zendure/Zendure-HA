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


# ── ApiHA → mixed legacy + zenSDK ────────────────────────────────────────────

CLOUD_WITH_HYPER = {
    "code": 200,
    "success": True,
    "data": {
        "mqtt": {"clientId": "c1", "url": "mqtt.zendure.tech:1883", "username": "u", "password": "p"},
        "deviceList": [
            {"deviceKey": "HYP001", "productModel": "hyper2000", "deviceName": "Hyper 2000",
             "snNumber": "HYP001", "ip": ""},
        ],
    },
    "msg": "Operation successful",
}


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


# ── ZenSdkMqttSetup ──────────────────────────────────────────────────────────

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
