"""device_by_identifier prefers the entry-scoped lookup of HA >= 2026.8 and falls back to the deprecated one; packs link to their parent on either core."""

from types import SimpleNamespace

import pytest
from homeassistant.helpers import device_registry as dr

from custom_components.zendure_ha.const import DOMAIN
from custom_components.zendure_ha.entity import device_by_identifier
from custom_components.zendure_ha.zendure.battery import ZendureBattery

SENTINEL = object()


class FakeRegistry:
    # records which lookup was used; without new_api it looks like a core before 2026.8
    def __init__(self, new_api: bool) -> None:
        self.scoped: list = []
        self.wide: list = []
        if new_api:
            self.async_get_device_by_identifier = lambda identifier, entry_id: (self.scoped.append((identifier, entry_id)), SENTINEL)[1]

    def async_get_device(self, identifiers=None):  # noqa: ANN001, ANN201
        self.wide.append(identifiers)
        return SENTINEL


async def test_finds_device_on_the_installed_core(hass, entry, device_registry: dr.DeviceRegistry) -> None:  # noqa: ANN001
    created = device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "SN123")}, name="SF800_23")
    assert device_by_identifier(hass, "SN123") is created
    assert device_by_identifier(hass, "nope") is None


async def test_prefers_entry_scoped_lookup(hass, entry, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    fake = FakeRegistry(new_api=True)
    monkeypatch.setattr(dr, "async_get", lambda _hass: fake)
    assert device_by_identifier(hass, "SN123") is SENTINEL
    assert fake.scoped == [((DOMAIN, "SN123"), entry.entry_id)] and fake.wide == []


async def test_falls_back_without_the_new_api_or_without_an_entry(hass, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    fake = FakeRegistry(new_api=True)  # new api, but no entry to scope to
    monkeypatch.setattr(dr, "async_get", lambda _hass: fake)
    assert device_by_identifier(hass, "SN123") is SENTINEL
    assert fake.scoped == [] and fake.wide == [{(DOMAIN, "SN123")}]
    fake = FakeRegistry(new_api=False)
    monkeypatch.setattr(dr, "async_get", lambda _hass: fake)
    assert device_by_identifier(hass, "SN123") is SENTINEL
    assert fake.wide == [{(DOMAIN, "SN123")}]


async def test_pack_is_linked_under_its_parent(hass, entry, device_registry: dr.DeviceRegistry) -> None:  # noqa: ANN001
    # end to end on the installed core: whichever via key the core takes, the pack ends up below the SolarFlow
    parent_entry = device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "parentdev")}, name="SF800_12")
    driver = SimpleNamespace(entry_id=entry.entry_id, manufacturer="Zendure", devices={})
    parent = SimpleNamespace(driver=driver, subentry_id=None, device_id="parentdev", battery_names={}, config_changed=lambda: None)
    pack = ZendureBattery(hass, "CO1F45678912", parent)
    assert device_by_identifier(hass, pack.device_id).via_device_id == parent_entry.id
    ZendureBattery._registry.clear()
