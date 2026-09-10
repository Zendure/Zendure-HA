"""Tests for entity.link_via_device.

DeviceInfo's `via_device` (an identifier tuple) is deprecated and removed in
HA 2027.8.0; its replacement `via_device_id` (a registry id) only exists from
HA 2026.8.0, and older cores reject the key with a TypeError that fails the
entity add. link_via_device therefore sends `via_device_id` where the core
takes it and `via_device` elsewhere.

The two differ in more than name: the core resolved `via_device` itself when
the entity was added, while `via_device_id` has to be looked up when the
device is constructed. If the parent is not registered by then the core would
raise on an unresolvable id, so the link is skipped instead.

As with device_by_identifier, CI pins one HA, so the branch that pin cannot
reach is exercised by faking the core's capability; the end-to-end test runs
against the real registry on whichever core is installed.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zendure_ha import entity as entity_mod
from custom_components.zendure_ha.const import DOMAIN
from custom_components.zendure_ha.entity import EntityDevice, link_via_device


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Add a Zendure config entry, as the entry-scoped parent lookup needs one."""
    mock_entry = MockConfigEntry(domain=DOMAIN)
    mock_entry.add_to_hass(hass)
    return mock_entry


@pytest.fixture
def parent(entry: MockConfigEntry, device_registry: dr.DeviceRegistry) -> dr.DeviceEntry:
    """A registered parent device, as the hub is when its batteries show up."""
    return device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "HUB1")}, name="Hyper 2000")


def fake_core(monkeypatch: pytest.MonkeyPatch, has_via_device_id: bool) -> None:
    monkeypatch.setattr(entity_mod, "core_has_via_device_id", lambda: has_via_device_id)


async def test_new_core_links_by_registry_id(hass: HomeAssistant, parent: dr.DeviceEntry, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_core(monkeypatch, has_via_device_id=True)
    info = DeviceInfo(identifiers={(DOMAIN, "PACK1")})

    link_via_device(hass, info, "HUB1")

    assert info["via_device_id"] == parent.id
    assert "via_device" not in info


async def test_new_core_skips_link_when_parent_is_not_registered(
    hass: HomeAssistant,
    entry: MockConfigEntry,  # noqa: ARG001 -- loaded so the lookup is entry-scoped, as in production
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An unresolvable via_device_id makes async_get_or_create raise, which would
    # fail the whole entity add -- dropping the link is the lesser evil.
    fake_core(monkeypatch, has_via_device_id=True)
    info = DeviceInfo(identifiers={(DOMAIN, "PACK1")}, name="AB2000 12345")

    link_via_device(hass, info, "HUB1")

    assert "via_device_id" not in info
    assert "via_device" not in info
    assert "HUB1" in caplog.text
    assert "AB2000 12345" in caplog.text


async def test_old_core_keeps_the_identifier_link(
    hass: HomeAssistant,
    parent: dr.DeviceEntry,  # noqa: ARG001 -- registered, yet the old core wants the identifier, not the id
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_core(monkeypatch, has_via_device_id=False)
    info = DeviceInfo(identifiers={(DOMAIN, "PACK1")})

    link_via_device(hass, info, "HUB1")

    assert info["via_device"] == (DOMAIN, "HUB1")
    assert "via_device_id" not in info


def test_capability_probe_matches_the_installed_core() -> None:
    # Whatever the probe says must agree with what the core actually accepts.
    assert entity_mod.core_has_via_device_id() == ("via_device_id" in DeviceInfo.__annotations__)


async def test_child_device_ends_up_under_its_parent(hass: HomeAssistant, entry: MockConfigEntry, parent: dr.DeviceEntry, device_registry: dr.DeviceRegistry) -> None:
    """End to end on the installed core: whichever key we send, the registry links the devices.

    On a core that deprecates `via_device`, report_usage raises when the offending
    call has no integration frame above it -- as here, where the test calls the
    registry directly. So on HA >= 2026.8.0 still sending the old key does not
    merely warn, it fails this test.
    """
    child = EntityDevice(hass, "PACK1", "AB2000 12345", "AB2000", sn="PACK1", parent="HUB1")

    registered = device_registry.async_get_or_create(config_entry_id=entry.entry_id, **child.attr_device_info)

    assert registered.via_device_id == parent.id
