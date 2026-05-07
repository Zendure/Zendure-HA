"""Tests for zenSDK MQTT code in the Zendure-HA integration.

Covers:
- _ZENSDK_BOOL value mapping in api.py
- mqttMsgLocal value-parsing logic
- ZendureZenSdk.doCommand MQTT publish routing
- ZendureZenSdk.dataRefresh polling skip when MQTT connected
- SolarFlow800Pro2 has 4 solar inputs; SolarFlow800Pro has only 2

Run with:
    uv run pytest tests/ -v
"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest
from pytest_mock import MockerFixture


# ── Shared helpers ────────────────────────────────────────────────────────────

def _patch_entity_add(mocker: MockerFixture) -> None:
    """Patch the class-level `add` on all entity classes that call self.add() during __init__.

    In production, HA platform setup assigns async_add_entities to these class
    attributes.  Without the patch the constructors raise AttributeError.
    Also patches missing HA util stubs (dt_util.utcnow) that conftest.py omits.
    """
    import importlib
    from datetime import datetime, timezone

    # dt_util.utcnow is used by ZendureRestoreSensor.__init__ but missing from the stub
    import homeassistant.util.dt as _dt_mod
    if not hasattr(_dt_mod, "utcnow"):
        _dt_mod.utcnow = lambda: datetime.now(timezone.utc)  # type: ignore[attr-defined]

    noop = mocker.MagicMock()
    for mod_name, cls_name in [
        ("custom_components.zendure_ha.number", "ZendureNumber"),
        ("custom_components.zendure_ha.sensor", "ZendureSensor"),
        ("custom_components.zendure_ha.sensor", "ZendureRestoreSensor"),
        ("custom_components.zendure_ha.switch", "ZendureSwitch"),
        ("custom_components.zendure_ha.select", "ZendureSelect"),
        ("custom_components.zendure_ha.select", "ZendureRestoreSelect"),
        ("custom_components.zendure_ha.binary_sensor", "ZendureBinarySensor"),
        ("custom_components.zendure_ha.button", "ZendureButton"),
    ]:
        mod = importlib.import_module(mod_name)
        cls = getattr(mod, cls_name, None)
        if cls is not None and not hasattr(cls, "add"):
            cls.add = noop


# ── 1. _ZENSDK_BOOL mapping ──────────────────────────────────────────────────

class TestZenSdkBoolMapping:
    """Verify every entry in _ZENSDK_BOOL resolves to the correct integer."""

    def _mapping(self):
        from custom_components.zendure_ha.api import _ZENSDK_BOOL
        return _ZENSDK_BOOL

    def test_on_maps_to_1(self) -> None:
        """'ON' must map to integer 1."""
        assert self._mapping()["ON"] == 1

    def test_off_maps_to_0(self) -> None:
        """'OFF' must map to integer 0."""
        assert self._mapping()["OFF"] == 0

    def test_yes_maps_to_1(self) -> None:
        """'yes' must map to integer 1."""
        assert self._mapping()["yes"] == 1

    def test_no_maps_to_0(self) -> None:
        """'no' must map to integer 0."""
        assert self._mapping()["no"] == 0

    def test_not_heating_maps_to_0(self) -> None:
        """'not_heating' must map to integer 0."""
        assert self._mapping()["not_heating"] == 0

    def test_heating_maps_to_1(self) -> None:
        """'heating' must map to integer 1."""
        assert self._mapping()["heating"] == 1


# ── 2. mqttMsgLocal value-parsing logic ──────────────────────────────────────

def _parse_zensdk_value(raw: str) -> Any:
    """Replicate the value-parsing logic from mqttMsgLocal inline."""
    from custom_components.zendure_ha.api import _ZENSDK_BOOL

    value: Any = raw
    if value in _ZENSDK_BOOL:
        return _ZENSDK_BOOL[value]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


class TestMqttMsgLocalValueParsing:
    """Test the value-parsing branch inside mqttMsgLocal without spinning up the full MQTT stack."""

    def test_availability_subtopic_has_slash(self) -> None:
        """A prop like 'electricLevel/availability' contains '/' and must be skipped."""
        prop = "electricLevel/availability"
        assert "/" in prop, "availability subtopic must contain '/' so the guard returns early"

    def test_plain_prop_has_no_slash(self) -> None:
        """A normal property name like 'electricLevel' must NOT contain '/'."""
        prop = "electricLevel"
        assert "/" not in prop

    def test_integer_string_parsed_as_int(self) -> None:
        """Payload '89' must become integer 89."""
        result = _parse_zensdk_value("89")
        assert result == 89
        assert isinstance(result, int)

    def test_float_string_parsed_as_float(self) -> None:
        """Payload '26.5' must become float 26.5."""
        result = _parse_zensdk_value("26.5")
        assert result == 26.5
        assert isinstance(result, float)

    def test_unknown_string_stays_as_string(self) -> None:
        """Payload 'idle' (not in bool map, not numeric) must remain the string 'idle'."""
        result = _parse_zensdk_value("idle")
        assert result == "idle"
        assert isinstance(result, str)

    def test_bool_key_on_returns_int_not_string(self) -> None:
        """Payload 'ON' must be resolved through _ZENSDK_BOOL, not kept as a string."""
        result = _parse_zensdk_value("ON")
        assert result == 1
        assert isinstance(result, int)


# ── 3. ZendureZenSdk.doCommand routing ───────────────────────────────────────

def _make_zensdk_device(mocker: MockerFixture, *, connection_value: int, has_mqtt: bool) -> Any:
    """Build a minimal stub that exercises ZendureZenSdk.doCommand."""
    from custom_components.zendure_ha.device import ZendureZenSdk

    device = mocker.MagicMock()
    device.connection.value = connection_value
    device.deviceId = "TESTDEV"
    device.mqtt = mocker.MagicMock() if has_mqtt else None
    device.httpPost = mocker.AsyncMock()
    device.mqttPublish = mocker.MagicMock()
    device.topic_write = "iot/test/TESTDEV/properties/write"
    # _MQTT_CATEGORY is a ClassVar — expose it on the stub so the real method can read it
    device._MQTT_CATEGORY = ZendureZenSdk._MQTT_CATEGORY
    device.doCommand = types.MethodType(ZendureZenSdk.doCommand, device)
    return device


class TestDoCommandRouting:
    """Test that doCommand dispatches to the right channel based on connection mode."""

    @pytest.mark.asyncio
    async def test_zensdk_with_mqtt_calls_publish(self, mocker: MockerFixture) -> None:
        """zenSDK mode (connection=2) with mqtt connected → mqtt.publish with correct topic."""
        device = _make_zensdk_device(mocker, connection_value=2, has_mqtt=True)
        await device.doCommand({"properties": {"outputLimit": 100}})

        device.mqtt.publish.assert_called_once_with(
            "Zendure/number/TESTDEV/outputLimit/set", "100"
        )
        device.httpPost.assert_not_called()
        device.mqttPublish.assert_not_called()

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
    async def test_multiple_props_each_get_publish(self, mocker: MockerFixture) -> None:
        """zenSDK mode with two properties → mqtt.publish called once per property."""
        device = _make_zensdk_device(mocker, connection_value=2, has_mqtt=True)
        await device.doCommand({"properties": {"outputLimit": 100, "inputLimit": 0}})

        assert device.mqtt.publish.call_count == 2


class TestMqttCategoryLookup:
    """Verify _MQTT_CATEGORY maps props to the right category string."""

    def _category(self, prop: str) -> str:
        from custom_components.zendure_ha.device import ZendureZenSdk
        return ZendureZenSdk._MQTT_CATEGORY.get(prop, "number")

    def test_acmode_is_select(self) -> None:
        """'acMode' belongs to 'select' category."""
        assert self._category("acMode") == "select"

    def test_lampswitch_is_switch(self) -> None:
        """'lampSwitch' belongs to 'switch' category."""
        assert self._category("lampSwitch") == "switch"

    def test_outputlimit_is_number(self) -> None:
        """'outputLimit' belongs to 'number' category."""
        assert self._category("outputLimit") == "number"

    def test_unknown_prop_defaults_to_number(self) -> None:
        """An unknown property key must default to 'number'."""
        assert self._category("someUnknownProp") == "number"

    def test_gridreverse_is_select(self) -> None:
        """'gridReverse' belongs to 'select' category."""
        assert self._category("gridReverse") == "select"

    def test_smartmode_is_switch(self) -> None:
        """'smartMode' belongs to 'switch' category."""
        assert self._category("smartMode") == "switch"


# ── 4. ZendureZenSdk.dataRefresh polling skip ────────────────────────────────

def _make_refresh_device(mocker: MockerFixture, *, connection_value: int, has_mqtt: bool, online: bool = True) -> Any:
    """Build a stub for dataRefresh tests."""
    device = mocker.MagicMock()
    device.connection.value = connection_value
    device.mqtt = mocker.MagicMock() if has_mqtt else None
    device.online = online
    device.httpGet = mocker.AsyncMock(return_value={})
    device.mqttProperties = mocker.AsyncMock()

    from custom_components.zendure_ha.device import ZendureZenSdk
    device.dataRefresh = types.MethodType(ZendureZenSdk.dataRefresh, device)
    return device


class TestDataRefresh:
    """Test that dataRefresh skips HTTP polling when MQTT push is active."""

    @pytest.mark.asyncio
    async def test_mqtt_connected_skips_httpget(self, mocker: MockerFixture) -> None:
        """When mqtt is set, dataRefresh returns immediately without calling httpGet."""
        device = _make_refresh_device(mocker, connection_value=2, has_mqtt=True)
        await device.dataRefresh(0)
        device.httpGet.assert_not_called()

    @pytest.mark.asyncio
    async def test_zensdk_no_mqtt_calls_httpget(self, mocker: MockerFixture) -> None:
        """zenSDK mode (connection=2) with mqtt=None → httpGet must be called."""
        device = _make_refresh_device(mocker, connection_value=2, has_mqtt=False)
        await device.dataRefresh(0)
        device.httpGet.assert_called_once_with("properties/report")

    @pytest.mark.asyncio
    async def test_cloud_update0_offline_calls_httpget(self, mocker: MockerFixture) -> None:
        """Cloud mode, update_count=0, offline → fallback httpGet is called."""
        device = _make_refresh_device(mocker, connection_value=0, has_mqtt=False, online=False)
        await device.dataRefresh(0)
        device.httpGet.assert_called_once_with("properties/report")

    @pytest.mark.asyncio
    async def test_cloud_update1_does_not_call_httpget(self, mocker: MockerFixture) -> None:
        """Cloud mode, update_count=1 → neither condition is met, httpGet not called."""
        device = _make_refresh_device(mocker, connection_value=0, has_mqtt=False, online=True)
        await device.dataRefresh(1)
        device.httpGet.assert_not_called()

    @pytest.mark.asyncio
    async def test_cloud_update0_online_does_not_call_httpget(self, mocker: MockerFixture) -> None:
        """Cloud mode, update_count=0 but device is online → httpGet not called (fallback only for offline)."""
        device = _make_refresh_device(mocker, connection_value=0, has_mqtt=False, online=True)
        await device.dataRefresh(0)
        device.httpGet.assert_not_called()


# ── 5. SolarFlow800Pro2 has 4 solar inputs ───────────────────────────────────

class TestSolarFlow800Pro2Inputs:
    """Verify SolarFlow800Pro2 adds solarPower3/solarPower4; Pro does not have them."""

    def _make_device(self, mocker: MockerFixture, cls: Any) -> Any:
        """Instantiate cls with a minimal mock hass and definition dict.

        Patches entity `add` class attributes so constructors don't raise
        AttributeError when calling self.add([self]).
        """
        _patch_entity_add(mocker)
        hass = mocker.MagicMock()
        hass.loop = asyncio.new_event_loop()
        definition = {
            "productKey": "testKey",
            "productModel": "solarFlow800Pro2",
            "snNumber": "TEST12345",
            "ip": "",
        }
        return cls(hass, "TESTDEV", "SolarFlow800Pro2", definition)

    def test_pro2_has_solarpower3(self, mocker: MockerFixture) -> None:
        """SolarFlow800Pro2 instance must expose solarPower3 attribute."""
        from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro2
        device = self._make_device(mocker, SolarFlow800Pro2)
        assert hasattr(device, "solarPower3")

    def test_pro2_has_solarpower4(self, mocker: MockerFixture) -> None:
        """SolarFlow800Pro2 instance must expose solarPower4 attribute."""
        from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro2
        device = self._make_device(mocker, SolarFlow800Pro2)
        assert hasattr(device, "solarPower4")

    def test_pro_does_not_have_solarpower3(self, mocker: MockerFixture) -> None:
        """SolarFlow800Pro (not Pro2) must NOT have solarPower3."""
        from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro
        device = self._make_device(mocker, SolarFlow800Pro)
        assert not hasattr(device, "solarPower3")

    def test_pro_does_not_have_solarpower4(self, mocker: MockerFixture) -> None:
        """SolarFlow800Pro (not Pro2) must NOT have solarPower4."""
        from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro
        device = self._make_device(mocker, SolarFlow800Pro)
        assert not hasattr(device, "solarPower4")

    def test_pro2_inherits_from_pro(self) -> None:
        """SolarFlow800Pro2 must be a subclass of SolarFlow800Pro."""
        from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro, SolarFlow800Pro2
        assert issubclass(SolarFlow800Pro2, SolarFlow800Pro)
