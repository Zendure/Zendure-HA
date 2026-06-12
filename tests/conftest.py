"""Test fixtures: stub Home Assistant and hardware modules so device.py can be imported without a full HA install."""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _module(name: str) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    return mod


class _Description:
    """Generic stand-in for HA EntityDescription dataclasses."""

    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


def _stub_homeassistant() -> None:
    if "homeassistant" in sys.modules:
        return

    ha = _module("homeassistant")

    core = _module("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    ha.core = core

    config_entries = _module("homeassistant.config_entries")
    config_entries.ConfigEntry = type("ConfigEntry", (), {})

    components = _module("homeassistant.components")
    bluetooth = _module("homeassistant.components.bluetooth")
    bluetooth.BluetoothServiceInfoBleak = type("BluetoothServiceInfoBleak", (), {})
    bluetooth.async_ble_device_from_address = lambda *_args, **_kwargs: None
    bluetooth.async_discovered_service_info = lambda *_args, **_kwargs: []
    components.bluetooth = bluetooth

    persistent_notification = _module(
        "homeassistant.components.persistent_notification"
    )
    persistent_notification.async_create = lambda *_args, **_kwargs: None
    components.persistent_notification = persistent_notification

    number = _module("homeassistant.components.number")
    number.NumberEntity = type("NumberEntity", (), {})
    number.NumberEntityDescription = _Description
    number.NumberMode = types.SimpleNamespace(SLIDER="slider", BOX="box", AUTO="auto")

    sensor = _module("homeassistant.components.sensor")
    sensor.SensorEntity = type("SensorEntity", (), {})
    sensor.SensorEntityDescription = _Description

    select = _module("homeassistant.components.select")
    select.SelectEntity = type("SelectEntity", (), {})
    select.SelectEntityDescription = _Description

    binary_sensor = _module("homeassistant.components.binary_sensor")
    binary_sensor.BinarySensorEntity = type("BinarySensorEntity", (), {})
    binary_sensor.BinarySensorEntityDescription = _Description

    button = _module("homeassistant.components.button")
    button.ButtonEntity = type("ButtonEntity", (), {})
    button.ButtonEntityDescription = _Description

    helpers = _module("homeassistant.helpers")
    device_registry = _module("homeassistant.helpers.device_registry")
    device_registry.DeviceEntry = type("DeviceEntry", (), {})
    device_registry.DeviceInfo = dict
    helpers.device_registry = device_registry

    entity_registry = _module("homeassistant.helpers.entity_registry")
    helpers.entity_registry = entity_registry

    restore_state = _module("homeassistant.helpers.restore_state")
    restore_state.RestoreEntity = type("RestoreEntity", (), {})
    helpers.restore_state = restore_state

    entity = _module("homeassistant.helpers.entity")
    entity.Entity = type("Entity", (), {})
    entity.EntityPlatformState = types.SimpleNamespace(
        ADDED="added", NOT_ADDED="not_added", REMOVED="removed"
    )
    helpers.entity = entity

    entity_platform = _module("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = type("AddEntitiesCallback", (), {})
    helpers.entity_platform = entity_platform

    template = _module("homeassistant.helpers.template")
    template.Template = type("Template", (), {})
    helpers.template = template

    aiohttp_client = _module("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda *_args, **_kwargs: None
    helpers.aiohttp_client = aiohttp_client

    util = _module("homeassistant.util")
    dt = _module("homeassistant.util.dt")
    from datetime import datetime

    dt.now = datetime.now
    dt.parse_datetime = lambda *_args, **_kwargs: None
    util.dt = dt
    ha.util = util


def _stub_hardware() -> None:
    if "paho" not in sys.modules:
        paho = _module("paho")
        paho_mqtt = _module("paho.mqtt")
        client = _module("paho.mqtt.client")
        client.Client = type("Client", (), {})
        paho_mqtt.client = client
        paho.mqtt = paho_mqtt

    if "bleak" not in sys.modules:
        bleak = _module("bleak")
        bleak.BleakClient = type("BleakClient", (), {})
        exc = _module("bleak.exc")
        exc.BleakError = type("BleakError", (Exception,), {})
        bleak.exc = exc


def _stub_packages() -> None:
    """Register custom_components packages without executing their __init__.py (which needs a full HA)."""
    if "custom_components" not in sys.modules:
        pkg = types.ModuleType("custom_components")
        pkg.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = pkg

    if "custom_components.zendure_ha" not in sys.modules:
        zpkg = types.ModuleType("custom_components.zendure_ha")
        zpkg.__path__ = [str(ROOT / "custom_components" / "zendure_ha")]
        sys.modules["custom_components.zendure_ha"] = zpkg


_stub_homeassistant()
_stub_hardware()
_stub_packages()
