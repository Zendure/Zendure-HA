"""Register homeassistant + paho stubs so tests run without HA installed.

MagicMock is fine for non-inherited objects (functions, constants).
For HA classes the integration *inherits from*, we need real Python classes —
Python's MRO/metaclass machinery breaks when you try to subclass MagicMock.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock


# ── Stub base-classes (real Python classes, not MagicMock) ───────────────────

class _Entity:
    entity_id: str = ""
    hass: object = None
    _attr_state: object = None
    _attr_name: str = ""
    _attr_unique_id: str = ""

    def __init__(self, *a: object, **kw: object) -> None: ...
    def __init_subclass__(cls, **kw: object) -> None: ...
    async def async_write_ha_state(self) -> None: ...
    def async_schedule_update_ha_state(self, *a: object) -> None: ...


class _RestoreEntity(_Entity):
    async def async_get_last_state(self) -> None: ...


class _RestoreSensor(_Entity):
    async def async_get_last_sensor_data(self) -> None: ...
    async def async_get_last_state(self) -> None: ...


class _RestoreNumber(_Entity):
    async def async_get_last_number_data(self) -> None: ...


class _RestoreSelect(_Entity):
    async def async_get_last_state(self) -> None: ...


# ── Helper ───────────────────────────────────────────────────────────────────

def _stub(name: str, **attrs: object) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# ── paho-mqtt ────────────────────────────────────────────────────────────────
_stub("paho")
_stub("paho.mqtt")
_stub("paho.mqtt.client", Client=MagicMock)
_stub("paho.mqtt.enums",
      CallbackAPIVersion=MagicMock(VERSION2=2),
      MQTTProtocolVersion=MagicMock(MQTTv31=3))

# ── bleak ────────────────────────────────────────────────────────────────────
_stub("bleak", BleakClient=MagicMock)
_stub("bleak.exc", BleakError=Exception)
_stub("bleak_retry_connector", establish_connection=AsyncMock)

# ── voluptuous ───────────────────────────────────────────────────────────────
_vol = _stub("voluptuous")
_vol.Schema = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]
_vol.Required = MagicMock(side_effect=lambda k, **kw: k)  # type: ignore[attr-defined]
_vol.Optional = MagicMock(side_effect=lambda k, **kw: k)  # type: ignore[attr-defined]

# ── homeassistant ─────────────────────────────────────────────────────────────
_stub("homeassistant")
_stub("homeassistant.const", Platform=MagicMock())
_stub("homeassistant.core", HomeAssistant=MagicMock, callback=lambda f: f,
      Event=MagicMock, EventStateChangedData=MagicMock)
_stub("homeassistant.exceptions",
      HomeAssistantError=Exception, ServiceValidationError=Exception)
_stub("homeassistant.loader", async_get_integration=AsyncMock())
_stub("homeassistant.util")
_stub("homeassistant.util.dt", parse_datetime=MagicMock(return_value=None))

_stub("homeassistant.config_entries",
      ConfigEntry=MagicMock, ConfigFlow=_Entity,
      ConfigFlowResult=MagicMock, OptionsFlow=_Entity)

_ha_auth = _stub("homeassistant.auth")
_ha_auth.__path__ = []  # make it look like a package
_stub("homeassistant.auth.const", GROUP_ID_USER="system-users")
_stub("homeassistant.auth.providers")
_stub("homeassistant.auth.providers.homeassistant",
      HassAuthProvider=MagicMock, async_get_provider=MagicMock())

# helpers — use real base classes where inherited
_dr = _stub("homeassistant.helpers.device_registry",
            async_get=MagicMock(), DeviceEntry=MagicMock, DeviceInfo=dict)
_er = _stub("homeassistant.helpers.entity_registry", async_get=MagicMock())
_rs = _stub("homeassistant.helpers.restore_state",
            RestoreEntity=_RestoreEntity, ExtraStoredData=MagicMock)
_sel = _stub("homeassistant.helpers.selector",
             EntitySelector=MagicMock, TextSelector=MagicMock,
             TextSelectorConfig=MagicMock, TextSelectorType=MagicMock())
_ha_helpers = _stub("homeassistant.helpers",
      device_registry=_dr, entity_registry=_er, restore_state=_rs, selector=_sel)
_ha_helpers.__path__ = []  # make it look like a package
_stub("homeassistant.helpers.aiohttp_client", async_get_clientsession=MagicMock())
_stub("homeassistant.helpers.storage", Store=MagicMock)
class _DataUpdateCoordinator:
    def __init__(self, *a: object, **kw: object) -> None: ...
    def __class_getitem__(cls, item: object) -> type: return cls

_stub("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=_DataUpdateCoordinator)
_stub("homeassistant.helpers.entity",
      Entity=_Entity, EntityPlatformState=MagicMock)
_stub("homeassistant.helpers.entity_platform", AddEntitiesCallback=MagicMock)
_stub("homeassistant.helpers.event", async_track_state_change_event=MagicMock())
_stub("homeassistant.helpers.http")
_stub("homeassistant.helpers.template", Template=MagicMock)

# components — entity base classes must be real Python classes
_stub("homeassistant.components")
_stub("homeassistant.components.bluetooth", async_discovered_service_info=MagicMock())
_stub("homeassistant.components.persistent_notification", async_create=AsyncMock())
_stub("homeassistant.components.number",
      NumberEntity=_Entity, NumberEntityDescription=MagicMock,
      NumberMode=MagicMock(), RestoreNumber=_RestoreNumber)
_stub("homeassistant.components.binary_sensor",
      BinarySensorEntity=_Entity, BinarySensorEntityDescription=MagicMock)
_stub("homeassistant.components.button",
      ButtonEntity=_Entity, ButtonEntityDescription=MagicMock)
_stub("homeassistant.components.sensor",
      SensorEntity=_Entity, SensorEntityDescription=MagicMock,
      SensorStateClass=MagicMock(), RestoreSensor=_RestoreSensor)
_stub("homeassistant.components.select",
      SelectEntity=_Entity, SelectEntityDescription=MagicMock,
      RestoreSelect=_RestoreSelect)
_stub("homeassistant.components.switch",
      SwitchEntity=_Entity, SwitchEntityDescription=MagicMock)
_stub("homeassistant.components.auth")
_stub("homeassistant.components.auth.providers")
_stub("homeassistant.components.auth.providers.homeassistant",
      HassAuthProvider=MagicMock, async_get_provider=MagicMock())
