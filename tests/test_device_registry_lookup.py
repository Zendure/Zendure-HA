"""Tests for entity.device_by_identifier.

`device_registry.async_get_device` is deprecated (removed in HA 2027.8.0)
because identifiers are no longer unique across config entries. The
replacement, `async_get_device_by_identifier`, is entry-scoped and only
exists from HA 2026.8.0, while this integration still supports older cores
(hacs.json declares 2025.4.0) -- so device_by_identifier prefers the new API
and falls back to the old one.

Both branches are covered on purpose: CI pins a single HA, so whichever
branch that pin does not reach would otherwise go unverified. The fake
registry below stands in for the core we are not running.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zendure_ha.const import DOMAIN
from custom_components.zendure_ha.entity import device_by_identifier

SENTINEL = object()


class FakeRegistry:
    """Records which lookup was used. Without new_api it looks like HA < 2026.8.0."""

    def __init__(self, new_api: bool) -> None:
        self.scoped: list[tuple[tuple[str, str], str]] = []
        self.registry_wide: list[set[tuple[str, str]] | None] = []
        if new_api:
            self.async_get_device_by_identifier = self._scoped

    def _scoped(self, identifier: tuple[str, str], config_entry_id: str) -> object:
        self.scoped.append((identifier, config_entry_id))
        return SENTINEL

    def async_get_device(self, identifiers: set[tuple[str, str]] | None = None) -> object:
        self.registry_wide.append(identifiers)
        return SENTINEL


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Add a Zendure config entry; the entry-scoped lookup needs one."""
    mock_entry = MockConfigEntry(domain=DOMAIN)
    mock_entry.add_to_hass(hass)
    return mock_entry


def patch_registry(monkeypatch: pytest.MonkeyPatch, new_api: bool) -> FakeRegistry:
    fake = FakeRegistry(new_api)
    monkeypatch.setattr(dr, "async_get", lambda _hass: fake)
    return fake


async def test_finds_device_registered_under_our_domain(
    hass: HomeAssistant, entry: MockConfigEntry, device_registry: dr.DeviceRegistry
) -> None:
    created = device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "SN123")}, name="Hyper 2000")

    assert device_by_identifier(hass, "SN123") is created
    assert device_by_identifier(hass, "nope") is None


async def test_prefers_entry_scoped_lookup_when_core_offers_it(hass: HomeAssistant, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = patch_registry(monkeypatch, new_api=True)

    assert device_by_identifier(hass, "SN123") is SENTINEL
    assert fake.scoped == [((DOMAIN, "SN123"), entry.entry_id)]
    assert fake.registry_wide == []


async def test_falls_back_when_core_lacks_the_replacement(hass: HomeAssistant, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = patch_registry(monkeypatch, new_api=False)

    assert device_by_identifier(hass, "SN123") is SENTINEL
    assert fake.registry_wide == [{(DOMAIN, "SN123")}]


async def test_falls_back_when_no_config_entry_is_loaded(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    # No `entry` fixture: the entry-scoped call has no entry id to scope to.
    fake = patch_registry(monkeypatch, new_api=True)

    assert device_by_identifier(hass, "SN123") is SENTINEL
    assert fake.scoped == []
    assert fake.registry_wide == [{(DOMAIN, "SN123")}]
