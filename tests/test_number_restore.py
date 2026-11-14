"""Tests for ZendureRestoreNumber restoring its value after an HA restart (#1549).

ZendureNumber.__init__ registers the entity through the platform's add callback
(`self.add([self])`). Home Assistant schedules that as an *eager* task, so
async_added_to_hass - and with it the restore - can complete before the
subclass constructor returns. Anything ZendureRestoreNumber.__init__ assigns
after `super().__init__()` then overwrites the restored value: the UI shows the
restored value while the manager reads the default (e.g. manual_power = 0).
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from homeassistant.components.number import NumberMode
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.entity_platform import EntityPlatform
from pytest_homeassistant.common import mock_restore_cache

from custom_components.zendure_ha.entity import EntityDevice
from custom_components.zendure_ha.number import ZendureNumber, ZendureRestoreNumber

ENTITY_ID = "number.zendure_manager_manual_power"


@pytest.fixture
def number_platform(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> EntityPlatform:
    """Route ZendureNumber.add through a real EntityPlatform, as number.async_setup_entry does."""
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="number",
        platform_name="zendure_ha",
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    # This is the callback HA hands to async_setup_entry as `async_add_entities`.
    monkeypatch.setattr(ZendureNumber, "add", staticmethod(platform._async_schedule_add_entities))  # noqa: SLF001
    return platform


def _make_manual_power(hass: HomeAssistant) -> ZendureRestoreNumber:
    manager = EntityDevice(hass, "Zendure Manager", "Zendure Manager")
    return ZendureRestoreNumber(manager, "manual_power", None, None, "W", "power", 12000, -12000, NumberMode.BOX, True)


@pytest.mark.usefixtures("number_platform")
async def test_restored_value_is_the_value_the_manager_reads(hass: HomeAssistant) -> None:
    mock_restore_cache(hass, [State(ENTITY_ID, "100")])

    number = _make_manual_power(hass)
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == "100"
    assert number.asNumber == 100


@pytest.mark.usefixtures("number_platform")
async def test_restored_negative_value_is_kept(hass: HomeAssistant) -> None:
    mock_restore_cache(hass, [State(ENTITY_ID, "-800")])

    number = _make_manual_power(hass)
    await hass.async_block_till_done()

    assert number.asNumber == -800


@pytest.mark.usefixtures("number_platform")
async def test_defaults_to_zero_without_restored_state(hass: HomeAssistant) -> None:
    number = _make_manual_power(hass)
    await hass.async_block_till_done()

    assert number.asNumber == 0
    assert hass.states.get(ENTITY_ID).state == "0"
