"""Regression tests for #1620: two devices of the same model/name must not
collide on unique_id, neither when creating entities nor when check_entities()
migrates pre-existing entity registry entries onto the current naming scheme.
"""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zendure_ha.const import DOMAIN
from custom_components.zendure_ha.entity import EntityDevice, EntityZendure, snakecase


def test_unique_id_disambiguated_by_serial_when_names_collide(hass) -> None:  # noqa: ANN001
    """Two devices sharing the same display name (e.g. two identical SolarFlow
    2400 AC+ hosts on one account, as in #1620) must still produce distinct
    unique_ids for the same property key.
    """
    device1 = EntityDevice(hass, "device-1", "SolarFlow 2400 AC+", sn="HEC4NEP1P021526")
    device2 = EntityDevice(hass, "device-2", "SolarFlow 2400 AC+", sn="HEC4NENCN521711")

    entity1 = EntityZendure(device1, "electricLevel")
    entity2 = EntityZendure(device2, "electricLevel")

    assert entity1.unique_id != entity2.unique_id
    # unique_id is built from device.sn (falling back to deviceId, then name) -
    # assert it actually reflects each device's own serial, not some other value.
    expected1 = snakecase(f"{device1.name.lower()}_{device1.sn}_electricLevel")
    expected2 = snakecase(f"{device2.name.lower()}_{device2.sn}_electricLevel")
    assert entity1.unique_id == expected1
    assert entity2.unique_id == expected2


async def test_check_entities_migration_does_not_collide_for_devices_with_same_name(hass) -> None:  # noqa: ANN001
    """Reproduces the crash reported alongside #1620: on restart,
    EntityDevice.__init__ calls check_entities() to migrate each device's
    existing registry entries onto the current naming scheme. Before this fix
    that target name was built from device.name only, so two identically
    named devices both migrated onto the *same* target unique_id/entity_id -
    the second device's migration then raised because the entity registry
    already had an entry with that unique_id, and the whole EntityDevice
    construction for that device aborted (losing all of its entities).
    """
    await EntityDevice.async_load_translations(hass)

    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    name = "SolarFlow 2400 AC+"
    devices = [
        ("device-1", "HEC4NEP1P021526", "dev1_old_electric_level"),
        ("device-2", "HEC4NENCN521711", "dev2_old_electric_level"),
    ]

    device_ids = {}
    for device_id, sn, old_object_id in devices:
        di = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, device_id), (DOMAIN, sn)},
            name=name,
        )
        device_ids[sn] = di.id
        # Pre-existing entity as it would exist before the current naming scheme
        # migrates it - a distinct id per device, as produced by an older
        # integration version (see #1406).
        entity_registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{old_object_id}_unique",
            suggested_object_id=old_object_id,
            device_id=di.id,
            config_entry=entry,
            translation_key="electric_level",
            has_entity_name=True,
        )

    # Constructing EntityDevice for each physical device runs check_entities(),
    # which must not raise even though both devices share the same `name`.
    for device_id, sn, _old_object_id in devices:
        EntityDevice(hass, device_id, name, sn=sn)

    migrated_unique_ids = set()
    for _device_id, sn, _old_object_id in devices:
        entries = er.async_entries_for_device(entity_registry, device_ids[sn], True)
        assert len(entries) == 1
        migrated_unique_ids.add(entries[0].unique_id)

    # Each device's entity ended up with its own unique_id - no collision, and
    # therefore no entity lost.
    assert len(migrated_unique_ids) == len(devices)
