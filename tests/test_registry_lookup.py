"""Regression tests for the device registry APIs deprecated in HA 2026.11."""

from __future__ import annotations

import logging

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from pytest_homeassistant.common import MockConfigEntry

from custom_components.zendure_ha.const import DOMAIN
from custom_components.zendure_ha.entity import EntityDevice, device_by_identifier
from custom_components.zendure_ha.migration import Migration

SENTINEL = object()


class FakeRegistry:
    """Records which lookup an integration used."""

    def __init__(self, new_api: bool, new_devices: bool = False) -> None:
        self.scoped: list[tuple[tuple[str, str], str]] = []
        self.wide: list[set[tuple[str, str]] | None] = []
        self.all: list[dict[str, object]] = []
        if new_api:
            self.async_get_device_by_identifier = lambda identifier, entry_id: (self.scoped.append((identifier, entry_id)), SENTINEL)[1]
        if new_devices:
            self.async_get_devices = lambda **kwargs: (self.all.append(kwargs), [SENTINEL])[1]

    def async_get_device(self, identifiers: set[tuple[str, str]] | None = None) -> object:
        self.wide.append(identifiers)
        return SENTINEL


def _registry_deprecations(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if "device_registry.async_get_device" in record.getMessage() or "deprecated `via_device` parameter" in record.getMessage()
    ]


@pytest.fixture(autouse=True)
def _clear_reported_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    from homeassistant.helpers import frame

    monkeypatch.setattr(frame, "_REPORTED_INTEGRATIONS", set())


@pytest.fixture
def entry(hass) -> MockConfigEntry:  # noqa: ANN001
    entry = MockConfigEntry(domain=DOMAIN, title="Zendure")
    entry.add_to_hass(hass)
    return entry


async def test_device_by_identifier_finds_the_device(hass, entry, device_registry: dr.DeviceRegistry, caplog: pytest.LogCaptureFixture) -> None:  # noqa: ANN001
    created = device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "SN123")}, name="SF800_123")

    with caplog.at_level(logging.WARNING):
        assert device_by_identifier(hass, "SN123") is created
        assert device_by_identifier(hass, "missing") is None

    assert _registry_deprecations(caplog) == []


async def test_device_by_identifier_prefers_the_entry_scoped_lookup(hass, entry, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    fake = FakeRegistry(new_api=True)
    monkeypatch.setattr(dr, "async_get", lambda _hass: fake)

    assert device_by_identifier(hass, "SN123") is SENTINEL
    assert fake.scoped == [((DOMAIN, "SN123"), entry.entry_id)]
    assert fake.wide == []


async def test_device_by_identifier_falls_back_without_the_new_api_or_entry(hass, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    fake = FakeRegistry(new_api=True)
    monkeypatch.setattr(dr, "async_get", lambda _hass: fake)

    assert device_by_identifier(hass, "SN123") is SENTINEL
    assert fake.scoped == []
    assert fake.wide == [{(DOMAIN, "SN123")}]

    fake = FakeRegistry(new_api=False)
    monkeypatch.setattr(dr, "async_get", lambda _hass: fake)

    assert device_by_identifier(hass, "SN123") is SENTINEL
    assert fake.wide == [{(DOMAIN, "SN123")}]


async def test_device_by_identifier_uses_the_new_lookup_without_an_entry(hass, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    fake = FakeRegistry(new_api=True, new_devices=True)
    monkeypatch.setattr(dr, "async_get", lambda _hass: fake)

    assert device_by_identifier(hass, "SN123") is SENTINEL
    assert fake.scoped == []
    assert fake.all == [{"identifiers": {(DOMAIN, "SN123")}}]
    assert fake.wide == []


async def test_migration_check_device_finds_the_existing_device(hass, entry, device_registry: dr.DeviceRegistry, caplog: pytest.LogCaptureFixture) -> None:  # noqa: ANN001
    created = device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "device-1")}, name="Old name")

    with caplog.at_level(logging.WARNING):
        Migration.check_device(hass, "device-1", "New Name", "SolarFlow 2400 AC", "SN123")

    updated = device_registry.async_get(created.id)
    assert updated is not None
    assert updated.name_by_user == "New Name"
    assert _registry_deprecations(caplog) == []


async def test_entity_device_lookup_keeps_the_existing_connections(hass, entry, device_registry: dr.DeviceRegistry, caplog: pytest.LogCaptureFixture) -> None:  # noqa: ANN001
    created = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "SN123")},
        connections={(dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:FF")},
        name="Old name",
    )

    with caplog.at_level(logging.WARNING):
        device = EntityDevice(hass, "device-1", "Name", "model", "", "SN123")

    assert device.attr_device_info["connections"] == created.connections
    assert _registry_deprecations(caplog) == []


async def test_update_version_finds_the_existing_device(hass, entry, device_registry: dr.DeviceRegistry, caplog: pytest.LogCaptureFixture) -> None:  # noqa: ANN001
    created = device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "SN123")}, name="Old name")
    device = EntityDevice(hass, "device-1", "Name", "model", "", "SN123")

    with caplog.at_level(logging.WARNING):
        device.updateVersion("1.2.3")

    updated = device_registry.async_get(created.id)
    assert updated is not None
    assert updated.sw_version == "1.2.3"
    assert _registry_deprecations(caplog) == []


async def test_child_device_links_to_its_parent(hass, entry, device_registry: dr.DeviceRegistry, caplog: pytest.LogCaptureFixture) -> None:  # noqa: ANN001
    parent = device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "PARENT")}, name="Parent")

    with caplog.at_level(logging.WARNING):
        child = EntityDevice(hass, "child", "Child", "model", "", "CHILD", "PARENT")
        if "via_device_id" in DeviceInfo.__optional_keys__:
            assert child.attr_device_info["via_device_id"] == parent.id
            assert "via_device" not in child.attr_device_info
        else:
            assert child.attr_device_info["via_device"] == (DOMAIN, "PARENT")
        linked = device_registry.async_get_or_create(config_entry_id=entry.entry_id, **child.attr_device_info)

    assert linked.via_device_id == parent.id
    assert _registry_deprecations(caplog) == []


async def test_child_device_finds_a_parent_registered_after_it(hass, entry, device_registry: dr.DeviceRegistry, caplog: pytest.LogCaptureFixture) -> None:  # noqa: ANN001
    child = EntityDevice(hass, "child", "Child", "model", "", "CHILD", "PARENT")
    parent = device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "PARENT")}, name="Parent")

    with caplog.at_level(logging.WARNING):
        linked = device_registry.async_get_or_create(config_entry_id=entry.entry_id, **child.attr_device_info)

    assert linked.via_device_id == parent.id
    assert _registry_deprecations(caplog) == []
