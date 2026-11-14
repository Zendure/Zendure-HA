"""Tests for the version-tolerant device registry helpers in registry.py.

HA 2026.8 deprecated DeviceRegistry.async_get_device(identifiers=...) and the
`via_device` device info key (both removed in 2027.8). The helpers must use the
new API on current HA without triggering the deprecation warnings, and keep
working on older HA versions (hacs.json still allows 2025.4).
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant.common import MockConfigEntry

from custom_components.zendure_ha.const import DOMAIN
from custom_components.zendure_ha.entity import EntityDevice
from custom_components.zendure_ha.registry import async_get_zendure_device, set_via_device

PARENT_SN = "HOA1TEST0001"


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Zendure")
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def parent_device(hass: HomeAssistant, config_entry: MockConfigEntry) -> dr.DeviceEntry:
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "parent-device-id"), (DOMAIN, PARENT_SN)},
        name="SolarFlow 800 Pro",
    )


def _deprecation_logged(caplog: pytest.LogCaptureFixture, what: str) -> bool:
    return any("deprecated" in record.getMessage() and what in record.getMessage() for record in caplog.records)


async def test_lookup_uses_config_entry_scoped_api(hass: HomeAssistant, parent_device, caplog) -> None:  # noqa: ANN001
    assert async_get_zendure_device(hass, PARENT_SN) == parent_device
    assert async_get_zendure_device(hass, "parent-device-id") == parent_device
    assert not _deprecation_logged(caplog, "async_get_device`")


@pytest.mark.usefixtures("config_entry")
async def test_lookup_returns_none_for_unknown_device(hass: HomeAssistant) -> None:
    assert async_get_zendure_device(hass, "does-not-exist") is None


async def test_lookup_falls_back_on_older_home_assistant(hass: HomeAssistant, parent_device, monkeypatch) -> None:  # noqa: ANN001
    # HA < 2026.8 has no async_get_device_by_identifier
    monkeypatch.delattr(dr.DeviceRegistry, "async_get_device_by_identifier")

    assert async_get_zendure_device(hass, PARENT_SN) == parent_device


async def test_via_device_id_is_used_when_parent_is_registered(hass: HomeAssistant, parent_device) -> None:  # noqa: ANN001
    device_info = dr.DeviceInfo(identifiers={(DOMAIN, "battery-sn")}, name="AB2000X")

    set_via_device(hass, device_info, PARENT_SN)

    assert device_info["via_device_id"] == parent_device.id
    assert "via_device" not in device_info


@pytest.mark.usefixtures("config_entry")
async def test_via_device_is_kept_while_parent_is_not_registered(hass: HomeAssistant) -> None:
    device_info = dr.DeviceInfo(identifiers={(DOMAIN, "battery-sn")}, name="AB2000X")

    set_via_device(hass, device_info, PARENT_SN)

    assert device_info["via_device"] == (DOMAIN, PARENT_SN)
    assert "via_device_id" not in device_info


@pytest.mark.usefixtures("parent_device")
async def test_via_device_is_kept_for_typeddict_device_info(hass: HomeAssistant) -> None:
    # On HA < 2026.8 DeviceInfo is a TypedDict, i.e. a plain dict at runtime
    device_info: dict = {"identifiers": {(DOMAIN, "battery-sn")}, "name": "AB2000X"}

    set_via_device(hass, device_info, PARENT_SN)

    assert device_info["via_device"] == (DOMAIN, PARENT_SN)
    assert "via_device_id" not in device_info


async def test_child_entity_device_links_to_parent_by_registry_id(hass: HomeAssistant, parent_device, caplog) -> None:  # noqa: ANN001
    battery = EntityDevice(hass, "battery-sn", "AB2000X", "AB2000X", "", "battery-sn", PARENT_SN)

    assert battery.attr_device_info["via_device_id"] == parent_device.id
    assert "via_device" not in battery.attr_device_info
    assert not _deprecation_logged(caplog, "async_get_device`")


async def test_update_version_writes_sw_version_to_registered_device(hass: HomeAssistant, parent_device) -> None:  # noqa: ANN001
    device = EntityDevice(hass, "parent-device-id", "SolarFlow 800 Pro", "SolarFlow 800 Pro", "", PARENT_SN)

    device.updateVersion("1.2.3")

    assert dr.async_get(hass).async_get(parent_device.id).sw_version == "1.2.3"
