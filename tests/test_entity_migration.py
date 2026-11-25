"""Tests for EntityDevice.check_entities entity registry migration (#1795)."""

from __future__ import annotations

import logging

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.zendure_ha.const import DOMAIN
from custom_components.zendure_ha.entity import EntityDevice


@pytest.fixture(autouse=True)
async def _load_full_translations(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure EntityDevice.checkEntity is freshly loaded from translations/en.json."""
    monkeypatch.setattr(EntityDevice, "checkEntity", {})
    await EntityDevice.async_load_translations(hass)


async def _setup_device_and_entity(
    hass: HomeAssistant,
    *,
    domain: str = "sensor",
    device_sn: str = "sn-1",
    device_name: str = "Zendure Manager",
    translation_key: str = "global_soc",
    unique_id: str = "zendure_manager_global_soc",
    entity_id: str | None = None,
) -> tuple[MockConfigEntry, dr.DeviceEntry, er.RegistryEntry]:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, device_sn)},
        name=device_name,
    )

    entity_registry = er.async_get(hass)
    reg_entry = entity_registry.async_get_or_create(
        domain,
        DOMAIN,
        unique_id,
        config_entry=entry,
        device_id=device.id,
        translation_key=translation_key,
    )
    if entity_id is not None and reg_entry.entity_id != entity_id:
        reg_entry = entity_registry.async_update_entity(
            reg_entry.entity_id,
            new_entity_id=entity_id,
        )

    return entry, device, reg_entry


async def test_migrates_entity_id_when_unique_id_already_matches(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only entity_id differs while unique_id already matches canonical (#1795)."""
    _entry, _device, reg_entry = await _setup_device_and_entity(
        hass,
        unique_id="zendure_manager_global_soc",
        entity_id="sensor.haupteingang_zendure_manager_zendure",
    )

    with caplog.at_level(logging.WARNING, logger="custom_components.zendure_ha.entity"):
        EntityDevice(hass, "Zendure Manager", "Zendure Manager", sn="sn-1")

    entity_registry = er.async_get(hass)
    migrated = entity_registry.async_get("sensor.zendure_manager_global_soc")
    assert migrated is not None
    assert migrated.id == reg_entry.id
    assert migrated.unique_id == "zendure_manager_global_soc"
    assert (
        entity_registry.async_get("sensor.haupteingang_zendure_manager_zendure") is None
    )
    assert "Could not migrate entity" not in caplog.text


async def test_migrates_unique_id_when_entity_id_already_matches(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only unique_id differs while entity_id already matches canonical."""
    _entry, _device, reg_entry = await _setup_device_and_entity(
        hass,
        unique_id="legacy_global_soc",
        entity_id="sensor.zendure_manager_global_soc",
    )

    with caplog.at_level(logging.WARNING, logger="custom_components.zendure_ha.entity"):
        EntityDevice(hass, "Zendure Manager", "Zendure Manager", sn="sn-1")

    entity_registry = er.async_get(hass)
    migrated = entity_registry.async_get("sensor.zendure_manager_global_soc")
    assert migrated is not None
    assert migrated.id == reg_entry.id
    assert migrated.unique_id == "zendure_manager_global_soc"
    assert "Could not migrate entity" not in caplog.text


async def test_migrates_both_entity_id_and_unique_id_when_both_differ(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both entity_id and unique_id differ from canonical."""
    _entry, _device, reg_entry = await _setup_device_and_entity(
        hass,
        unique_id="legacy_global_soc",
        entity_id="sensor.old_manager_global_soc",
    )

    with caplog.at_level(logging.WARNING, logger="custom_components.zendure_ha.entity"):
        EntityDevice(hass, "Zendure Manager", "Zendure Manager", sn="sn-1")

    entity_registry = er.async_get(hass)
    migrated = entity_registry.async_get("sensor.zendure_manager_global_soc")
    assert migrated is not None
    assert migrated.id == reg_entry.id
    assert migrated.unique_id == "zendure_manager_global_soc"
    assert entity_registry.async_get("sensor.old_manager_global_soc") is None
    assert "Could not migrate entity" not in caplog.text


async def test_genuine_target_conflict_logs_warning_without_blocking_other_entities(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unrelated target entity_id conflict logs a warning without blocking."""
    entry, device, conflicting_entry = await _setup_device_and_entity(
        hass,
        unique_id="zendure_manager_global_soc",
        entity_id="sensor.custom_global_soc",
    )
    entity_registry = er.async_get(hass)

    # An unrelated entry occupies the canonical entity_id.
    entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "unrelated_other_unique_id",
        config_entry=entry,
        suggested_object_id="zendure_manager_global_soc",
    )
    assert entity_registry.async_get("sensor.zendure_manager_global_soc") is not None

    # A second entity on the same device should still migrate cleanly.
    second_entry = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "legacy_grid_power",
        config_entry=entry,
        device_id=device.id,
        translation_key="grid_power",
    )
    second_entry = entity_registry.async_update_entity(
        second_entry.entity_id,
        new_entity_id="sensor.old_grid_power",
    )

    with caplog.at_level(logging.WARNING, logger="custom_components.zendure_ha.entity"):
        EntityDevice(hass, "Zendure Manager", "Zendure Manager", sn="sn-1")

    expected_warning = (
        "Could not migrate entity sensor.custom_global_soc -> "
        "sensor.zendure_manager_global_soc"
    )
    assert expected_warning in caplog.text
    unchanged = entity_registry.async_get("sensor.custom_global_soc")
    assert unchanged is not None
    assert unchanged.id == conflicting_entry.id

    migrated_second = entity_registry.async_get("sensor.zendure_manager_grid_power")
    assert migrated_second is not None
    assert migrated_second.id == second_entry.id
    assert migrated_second.unique_id == "zendure_manager_grid_power"


async def test_duplicate_entries_prefer_canonical_unique_id_and_drop_stale_loser(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Duplicate entries keep the canonical unique_id survivor (#1477)."""
    entry, device, stale_entry = await _setup_device_and_entity(
        hass,
        domain="number",
        device_name="SolarFlow 2400 AC+",
        translation_key="output_limit",
        unique_id="solarflow_2400_ac_output_limit",
        entity_id="number.solarflow_2400_ac_plus_output_limit",
    )
    entity_registry = er.async_get(hass)
    survivor = entity_registry.async_get_or_create(
        "number",
        DOMAIN,
        "solarflow_2400_ac_plus_output_limit",
        config_entry=entry,
        device_id=device.id,
        translation_key="output_limit",
    )
    survivor = entity_registry.async_update_entity(
        survivor.entity_id,
        new_entity_id="number.solarflow_2400_ac_plus_output_limit_2",
    )

    with caplog.at_level(logging.WARNING, logger="custom_components.zendure_ha.entity"):
        EntityDevice(hass, "SolarFlow 2400 AC+", "SolarFlow 2400 AC+", sn="sn-1")

    canonical = entity_registry.async_get("number.solarflow_2400_ac_plus_output_limit")
    assert canonical is not None
    assert canonical.id == survivor.id
    assert canonical.unique_id == "solarflow_2400_ac_plus_output_limit"
    assert entity_registry.async_get(stale_entry.entity_id) is None or (
        canonical.id == survivor.id
    )
    assert (
        entity_registry.async_get("number.solarflow_2400_ac_plus_output_limit_2")
        is None
    )
    assert "Could not migrate entity" not in caplog.text
