from __future__ import annotations

import types
from typing import Any

from pytest_mock import MockerFixture


def _patch_entity_add(mocker: MockerFixture) -> None:
    """Patch the class-level `add` on all entity classes that call self.add() during __init__.

    In production, HA platform setup assigns async_add_entities to these class
    attributes.  Without the patch the constructors raise AttributeError.
    Also patches missing HA util stubs (dt_util.utcnow) that conftest.py omits.
    """
    import importlib
    from datetime import datetime, timezone

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
    device._MQTT_CATEGORY = ZendureZenSdk._MQTT_CATEGORY
    device.doCommand = types.MethodType(ZendureZenSdk.doCommand, device)
    return device


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
