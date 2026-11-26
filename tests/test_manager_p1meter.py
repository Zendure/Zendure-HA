"""
Tests for ZendureManager P1 meter scaling factor updates (#1676).

When the configured P1 sensor changes at runtime (for example via the options
flow update listener) from a sensor reporting kW to one reporting W, the
manager must recalculate `p1_factor` back to 1 rather than keeping 1000. It
must also pick up the sensor's unit when a sensor that was missing or
unavailable at startup publishes its state later.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.zendure_ha import update_listener
from custom_components.zendure_ha.const import CONF_P1METER
from custom_components.zendure_ha.manager import ZendureManager

KW_FACTOR = 1000
W_FACTOR = 1


def _make_p1_manager(hass: HomeAssistant) -> ZendureManager:
    """Create a minimal ZendureManager wired to a real HomeAssistant state bus."""
    manager = object.__new__(ZendureManager)
    manager.hass = hass
    manager.p1meterEvent = None
    manager.p1_factor = W_FACTOR
    manager.p1_history = deque([25, -25], maxlen=8)
    manager.zero_fast = datetime.min
    manager.zero_next = datetime.min
    manager.charge = []
    manager.discharge = []
    manager.idle = []
    manager.fuseGroups = []
    manager.powerChanged = AsyncMock()
    return manager


async def _emit_state(
    hass: HomeAssistant,
    manager: ZendureManager,
    entity_id: str,
    state: str,
    attributes: dict[str, Any] | None = None,
) -> None:
    """Publish a P1 state update without hitting the inter-update rate limiter."""
    manager.zero_fast = datetime.min
    manager.zero_next = datetime.min
    hass.states.async_set(entity_id, state, attributes or {})
    await hass.async_block_till_done()


def _dispatched_p1_values(manager: ZendureManager) -> list[int]:
    return [call.args[0] for call in manager.powerChanged.await_args_list]


async def test_switching_from_kw_to_w_sensor_resets_p1_factor(
    hass: HomeAssistant,
) -> None:
    """Changing P1 meter from kW to W via update_listener resets p1_factor to 1."""
    hass.states.async_set("sensor.grid_kw", "0.5", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.grid_w", "400", {"unit_of_measurement": "W"})
    manager = _make_p1_manager(hass)

    manager.update_p1meter("sensor.grid_kw")
    assert manager.p1_factor == KW_FACTOR

    await _emit_state(
        hass, manager, "sensor.grid_kw", "0.6", {"unit_of_measurement": "kW"}
    )
    assert _dispatched_p1_values(manager) == [600]

    entry = SimpleNamespace(
        entry_id="entry-1", data={CONF_P1METER: "sensor.grid_w"}, runtime_data=manager
    )
    await update_listener(hass, entry)
    assert manager.p1_factor == W_FACTOR

    # Old kW sensor must be unsubscribed; new W sensor must not be multiplied by 1000.
    await _emit_state(
        hass, manager, "sensor.grid_kw", "0.9", {"unit_of_measurement": "kW"}
    )
    await _emit_state(
        hass, manager, "sensor.grid_w", "500", {"unit_of_measurement": "W"}
    )
    assert _dispatched_p1_values(manager) == [600, 500]


async def test_switching_to_sensor_without_unit_attribute_defaults_to_watts(
    hass: HomeAssistant,
) -> None:
    """A sensor omitting unit_of_measurement defaults to W (factor 1)."""
    hass.states.async_set("sensor.grid_kw", "0.5", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.grid_plain", "450", {})
    manager = _make_p1_manager(hass)

    manager.update_p1meter("sensor.grid_kw")
    assert manager.p1_factor == KW_FACTOR

    manager.update_p1meter("sensor.grid_plain")
    assert manager.p1_factor == W_FACTOR

    await _emit_state(hass, manager, "sensor.grid_plain", "350", {})
    assert _dispatched_p1_values(manager) == [350]


@pytest.mark.parametrize("unit", ["kW", "kilowatt", "kilowatts"])
async def test_switching_from_w_to_kw_variants_sets_factor_1000(
    hass: HomeAssistant, unit: str
) -> None:
    """Switching from a W sensor to any supported kW unit sets p1_factor to 1000."""
    hass.states.async_set("sensor.grid_w", "500", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.grid_kw", "1.25", {"unit_of_measurement": unit})
    manager = _make_p1_manager(hass)

    manager.update_p1meter("sensor.grid_w")
    assert manager.p1_factor == W_FACTOR

    manager.update_p1meter("sensor.grid_kw")
    assert manager.p1_factor == KW_FACTOR

    await _emit_state(
        hass, manager, "sensor.grid_kw", "0.75", {"unit_of_measurement": unit}
    )
    assert _dispatched_p1_values(manager) == [750]


@pytest.mark.parametrize("cleared", [None, ""])
async def test_clearing_p1meter_unsubscribes_and_resets_p1_factor(
    hass: HomeAssistant,
    cleared: str | None,
) -> None:
    """Clearing the P1 meter unsubscribes the listener and resets p1_factor to 1."""
    hass.states.async_set("sensor.grid_kw", "0.5", {"unit_of_measurement": "kW"})
    manager = _make_p1_manager(hass)

    manager.update_p1meter("sensor.grid_kw")
    assert manager.p1_factor == KW_FACTOR

    manager.update_p1meter(cleared)
    assert manager.p1_factor == W_FACTOR
    assert manager.p1meterEvent is None

    await _emit_state(
        hass, manager, "sensor.grid_kw", "0.8", {"unit_of_measurement": "kW"}
    )
    manager.powerChanged.assert_not_awaited()


async def test_sensor_missing_at_startup_adopts_kw_factor_when_state_arrives(
    hass: HomeAssistant,
) -> None:
    """A kW sensor missing at setup scales by 1000 on its first reading."""
    manager = _make_p1_manager(hass)

    manager.update_p1meter("sensor.late_kw")
    assert manager.p1_factor == W_FACTOR

    await _emit_state(
        hass, manager, "sensor.late_kw", "0.75", {"unit_of_measurement": "kW"}
    )
    assert manager.p1_factor == KW_FACTOR
    assert _dispatched_p1_values(manager) == [750]


async def test_switching_from_kw_to_unavailable_w_sensor_resets_factor(
    hass: HomeAssistant,
) -> None:
    """Switching from kW to an unavailable W sensor resets p1_factor immediately."""
    hass.states.async_set("sensor.grid_kw", "0.5", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.late_w", "unavailable", {})
    manager = _make_p1_manager(hass)

    manager.update_p1meter("sensor.grid_kw")
    assert manager.p1_factor == KW_FACTOR

    manager.update_p1meter("sensor.late_w")
    assert manager.p1_factor == W_FACTOR

    await _emit_state(
        hass, manager, "sensor.late_w", "350", {"unit_of_measurement": "W"}
    )
    assert manager.p1_factor == W_FACTOR
    assert _dispatched_p1_values(manager) == [350]


async def test_kw_sensor_becoming_unavailable_preserves_factor_and_recovers(
    hass: HomeAssistant,
) -> None:
    """A transient unavailable state without unit attributes does not drop p1_factor."""
    hass.states.async_set("sensor.grid_kw", "0.5", {"unit_of_measurement": "kW"})
    manager = _make_p1_manager(hass)

    manager.update_p1meter("sensor.grid_kw")
    assert manager.p1_factor == KW_FACTOR

    await _emit_state(hass, manager, "sensor.grid_kw", "unavailable", {})
    assert manager.p1_factor == KW_FACTOR
    manager.powerChanged.assert_not_awaited()

    await _emit_state(
        hass, manager, "sensor.grid_kw", "0.4", {"unit_of_measurement": "kW"}
    )
    assert manager.p1_factor == KW_FACTOR
    assert _dispatched_p1_values(manager) == [400]


async def test_same_sensor_changing_unit_from_kw_to_w_updates_factor(
    hass: HomeAssistant,
) -> None:
    """Changing the sensor's unit from kW to W updates p1_factor on state event."""
    hass.states.async_set("sensor.template_p1", "0.5", {"unit_of_measurement": "kW"})
    manager = _make_p1_manager(hass)
    manager.update_p1meter("sensor.template_p1")

    await _emit_state(
        hass, manager, "sensor.template_p1", "0.6", {"unit_of_measurement": "kW"}
    )
    assert manager.p1_factor == KW_FACTOR

    await _emit_state(
        hass, manager, "sensor.template_p1", "600", {"unit_of_measurement": "W"}
    )
    assert manager.p1_factor == W_FACTOR
    assert _dispatched_p1_values(manager) == [600, 600]
