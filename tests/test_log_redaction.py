"""Credentials must never end up in the log.

Users routinely paste home-assistant.log into GitHub issues, so every log line
that dumps an API reply, a device definition or a BLE/MQTT payload has to mask
passwords, usernames and tokens while keeping the rest of the diagnostics.
"""

from __future__ import annotations

import base64
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zendure_ha import api as api_module
from custom_components.zendure_ha.api import Api, ApiError
from custom_components.zendure_ha.const import DOMAIN
from custom_components.zendure_ha.manager import ZendureManager

MQTT_USER = "cloud-mqtt-user-7f3a"
MQTT_PASSWORD = "cloud-mqtt-psw-9c1e"  # noqa: S105
MQTT_CLIENT_ID = "cloud-client-id-4b2d"
LOCAL_USER = "local-broker-user-5e8f"
LOCAL_PASSWORD = "local-broker-psw-2a6c"  # noqa: S105
WIFI_SSID = "MyHomeWifi-SSID-3d9b"
WIFI_PASSWORD = "my-home-wifi-psw-8e4f"  # noqa: S105
SECRETS = (MQTT_USER, MQTT_PASSWORD, MQTT_CLIENT_ID, LOCAL_USER, LOCAL_PASSWORD, WIFI_SSID, WIFI_PASSWORD)


def _assert_no_secrets(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text
    assert "**REDACTED**" in text


def _cloud_mqtt() -> dict[str, Any]:
    return {"url": "mqtt.example.invalid:1883", "clientId": MQTT_CLIENT_ID, "username": MQTT_USER, "password": MQTT_PASSWORD}


def _device_definition() -> dict[str, Any]:
    return {
        "deviceKey": "device-1",
        "productModel": "Unsupported Test Model",
        "productKey": "testProdKey",
        "snNumber": "JO4ATEST1",
        "deviceName": "Test device",
        "server": "192.168.1.2",
        "port": 1883,
        "username": LOCAL_USER,
        "password": LOCAL_PASSWORD,
    }


class _FakeResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    async def json(self) -> dict[str, Any]:
        return self._data


class _FakeSession:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    async def post(self, **_kwargs: Any) -> _FakeResponse:
        return _FakeResponse(self._data)


@pytest.mark.parametrize(
    ("reply", "error_key"),
    [
        # rejected (success flag missing) - reply still carries the mqtt block
        ({"code": 500, "success": False, "msg": "error", "data": {"mqtt": _cloud_mqtt()}}, "api_rejected"),
        # no devices - the mqtt credentials are still part of the reply
        ({"code": 200, "success": True, "data": {"deviceList": [], "mqtt": _cloud_mqtt()}}, "no_devices"),
        # no mqtt info - the device list carries the local broker credentials
        ({"code": 200, "success": True, "data": {"deviceList": [_device_definition()], "mqtt": {}}}, "no_mqtt"),
    ],
)
async def test_api_error_replies_are_redacted(hass, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, reply: dict[str, Any], error_key: str) -> None:  # noqa: ANN001
    """Failed deviceList replies are logged, but without the MQTT credentials."""
    monkeypatch.setattr(api_module, "async_get_clientsession", lambda _hass: _FakeSession(reply))
    token = base64.b64encode(b"https://api.example.invalid.testAppKey").decode()

    with caplog.at_level(logging.INFO), pytest.raises(ApiError) as err:
        await Api.ApiHA(hass, {"token": token})

    assert err.value.key == error_key
    _assert_no_secrets(caplog.text)


async def test_ble_command_is_redacted(zensdk_device, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:  # noqa: ANN001
    """The BLE provisioning command keeps method/messageId in the log but masks the WiFi credentials."""
    monkeypatch.setattr(Api, "wifissid", WIFI_SSID)
    monkeypatch.setattr(Api, "wifipsw", WIFI_PASSWORD)
    client = SimpleNamespace(write_gatt_char=AsyncMock())
    command = {
        "iotUrl": "mqtt.example.invalid",
        "messageId": 1002,
        "method": "token",
        "password": Api.wifipsw,
        "ssid": Api.wifissid,
        "timeZone": "GMT+01:00",
        "token": "abcdefgh",
    }

    with caplog.at_level(logging.INFO):
        await zensdk_device.bleCommand(client, command)

    _assert_no_secrets(caplog.text)
    assert '"method": "token"' in caplog.text
    assert '"messageId": 1002' in caplog.text

    # The device itself must still receive the real credentials.
    written = bytes(client.write_gatt_char.await_args.args[1]).decode()
    assert json.loads(written) == command


async def test_adding_device_is_redacted(hass, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:  # noqa: ANN001
    """The 'Adding device' line keeps the device definition but masks the local broker credentials."""
    entry = MockConfigEntry(domain=DOMAIN, data={"token": "unused"})
    entry.add_to_hass(hass)

    async def _connect(_hass: Any, _data: dict[str, Any], _reload: bool) -> dict[str, Any]:
        return {"deviceList": [_device_definition()], "mqtt": _cloud_mqtt()}

    async def _integration(_hass: Any, _domain: str) -> Any:
        return SimpleNamespace(manifest={"version": "test"})

    async def _noop_async(*_args: Any) -> None:
        return

    monkeypatch.setattr(Api, "Connect", staticmethod(_connect))
    monkeypatch.setattr(Api, "Init", lambda *_args: None)
    monkeypatch.setattr("custom_components.zendure_ha.manager.async_get_integration", _integration)

    manager = ZendureManager(hass, entry)
    monkeypatch.setattr(manager, "update_fusegroups", _noop_async)
    monkeypatch.setattr(manager, "update_p1meter", lambda *_args: None)

    with caplog.at_level(logging.INFO):
        await manager.loadDevices()

    assert "Adding device: device-1 Unsupported Test Model" in caplog.text
    assert "JO4ATEST1" in caplog.text
    _assert_no_secrets(caplog.text)


@pytest.mark.parametrize("handler", ["mqttMsgCloud", "mqttMsgLocal"])
async def test_mqtt_logging_is_redacted(zensdk_device, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, handler: str) -> None:  # noqa: ANN001
    """With MQTT logging enabled, message payloads are logged with credentials masked."""
    monkeypatch.setattr(Api, "mqttLogging", True)
    monkeypatch.setattr(Api, "devices", {zensdk_device.deviceId: zensdk_device})
    payload = {"messageId": 7, "server": "mqtt.example.invalid", "username": LOCAL_USER, "password": LOCAL_PASSWORD}
    msg = SimpleNamespace(topic=f"iot/{zensdk_device.prodkey}/{zensdk_device.deviceId}/config", payload=json.dumps(payload).encode())

    with caplog.at_level(logging.INFO):
        getattr(Api(), handler)(object(), None, msg)

    assert "'messageId': 7" in caplog.text
    _assert_no_secrets(caplog.text)


async def test_register_replay_is_redacted(zensdk_device, caplog: pytest.LogCaptureFixture) -> None:  # noqa: ANN001
    """register/replay payloads are logged with credentials masked."""
    zensdk_device.mqtt = None
    payload = {"messageId": 3, "params": {"clientId": MQTT_CLIENT_ID, "password": MQTT_PASSWORD}}

    with caplog.at_level(logging.INFO):
        zensdk_device.mqttMessage("register/replay", payload)

    assert "Register replay for" in caplog.text
    _assert_no_secrets(caplog.text)
