"""Unit tests for EntityZendure.property_key and ZendureDevice.entityWrite().

Covers two aspects of the same fix:
  1. EntityZendure.__init__ stores the original camelCase key as property_key
     before it is converted to snake_case for unique_id.
  2. entityWrite() uses property_key (camelCase) in the MQTT payload,
     not a name derived from unique_id (which is snake_case).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# conftest stubs are already in sys.modules when pytest loads this file.
from custom_components.zendure_ha.device import ZendureDevice  # noqa: E402
from custom_components.zendure_ha.entity import EntityZendure  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _entity_device(name: str = "Hyper2000") -> SimpleNamespace:
    """Minimal fake EntityDevice compatible with EntityZendure.__init__."""
    return SimpleNamespace(name=name, entities={}, attr_device_info=None)


def _fake_entity(*, property_key: str, unique_id: str) -> SimpleNamespace:
    """Fake entity carrying the two attributes read by entityWrite()."""
    return SimpleNamespace(property_key=property_key, unique_id=unique_id, name="test")


def _fake_zdevice() -> SimpleNamespace:
    """Minimal fake ZendureDevice for entityWrite() tests."""
    return SimpleNamespace(
        name="Hyper2000",
        deviceId="device_001",
        _messageid=0,
        topic_write="test/write",
        mqtt=MagicMock(),
    )


# ══════════════════════════════════════════════════════════════════════════════
# EntityZendure.__init__ — property_key storage
# ══════════════════════════════════════════════════════════════════════════════


def test_property_key_stores_original_camelcase():
    """camelCase uniqueid → property_key equals the original string."""
    entity = EntityZendure(_entity_device(), "lampSwitch")
    assert entity.property_key == "lampSwitch"


def test_unique_id_is_snakecase():
    """camelCase uniqueid → _attr_unique_id is converted to snake_case."""
    entity = EntityZendure(_entity_device(), "lampSwitch")
    assert entity._attr_unique_id == "hyper2000_lamp_switch"


def test_property_key_differs_from_unique_id():
    """property_key (camelCase) and _attr_unique_id (snake_case) are distinct."""
    entity = EntityZendure(_entity_device(), "lampSwitch")
    assert entity.property_key != entity._attr_unique_id


# ══════════════════════════════════════════════════════════════════════════════
# ZendureDevice.entityWrite() — MQTT payload property name
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_entity_write_sends_camelcase_key():
    """entityWrite() uses property_key (camelCase) as the property name in the MQTT payload."""
    entity = _fake_entity(property_key="lampSwitch", unique_id="hyper2000_lamp_switch")
    device = _fake_zdevice()

    await ZendureDevice.entityWrite(device, entity, True)

    device.mqtt.publish.assert_called_once()
    _, payload_str = device.mqtt.publish.call_args.args
    payload = json.loads(payload_str)
    assert "lampSwitch" in payload["properties"]


@pytest.mark.asyncio
async def test_entity_write_does_not_use_snakecase_key():
    """entityWrite() does not send the snake_case form of the property name."""
    entity = _fake_entity(property_key="lampSwitch", unique_id="hyper2000_lamp_switch")
    device = _fake_zdevice()

    await ZendureDevice.entityWrite(device, entity, True)

    _, payload_str = device.mqtt.publish.call_args.args
    payload = json.loads(payload_str)
    assert "lamp_switch" not in payload["properties"]
