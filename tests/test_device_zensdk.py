"""Scenario tests for ZendureZenSdk.entityWrite / charge / discharge / _sendPower.

These cover the interaction that shipped as two separate regressions:
  - #1505: a bare outputLimit/inputLimit write is silently ignored once the
    device has dropped out of smart mode -> smartMode/acMode must be
    re-asserted (PR #1507).
  - #1521: re-asserting smartMode/acMode on *every* write makes the device
    flip mode on every command when writes repeat the same direction
    (PR #1538 debounces the re-assertion).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.zendure_ha.const import SmartMode


def _last_props(device) -> dict:  # noqa: ANN001
    return device.mqtt_commands[-1]["properties"]


async def test_discharge_first_call_asserts_full_command(zensdk_device) -> None:  # noqa: ANN001
    await zensdk_device.discharge(500)

    assert len(zensdk_device.mqtt_commands) == 1
    assert _last_props(zensdk_device) == {"outputLimit": 500, "inputLimit": 0, "smartMode": 1, "acMode": 2}


async def test_discharge_repeated_same_direction_does_not_reassert_mode(zensdk_device) -> None:  # noqa: ANN001
    """Regression test for #1521: repeated same-direction writes must not flip smartMode/acMode every time."""
    await zensdk_device.discharge(500)
    await zensdk_device.discharge(650)
    await zensdk_device.discharge(300)

    assert len(zensdk_device.mqtt_commands) == 3
    assert [c["properties"] for c in zensdk_device.mqtt_commands] == [
        {"outputLimit": 500, "inputLimit": 0, "smartMode": 1, "acMode": 2},
        {"outputLimit": 650, "inputLimit": 0},
        {"outputLimit": 300, "inputLimit": 0},
    ]


async def test_charge_after_discharge_reasserts_mode_on_direction_change(zensdk_device) -> None:  # noqa: ANN001
    """Switching direction must always re-assert smartMode/acMode, even right after a previous assertion."""
    await zensdk_device.discharge(500)
    await zensdk_device.charge(-400)

    props = _last_props(zensdk_device)
    assert props == {"outputLimit": 0, "inputLimit": 400, "smartMode": 1, "acMode": 1}


async def test_discharge_reasserts_mode_after_reassert_interval_elapses(zensdk_device) -> None:  # noqa: ANN001
    """A silent smartMode drop must still self-heal without requiring a direction change (preserves #1505 fix)."""
    await zensdk_device.discharge(500)
    assert "smartMode" in _last_props(zensdk_device)

    await zensdk_device.discharge(600)
    assert "smartMode" not in _last_props(zensdk_device)

    # Simulate MODE_REASSERT_INTERVAL having elapsed since the last assertion.
    zensdk_device._modeAssertedAt = datetime.now() - SmartMode.MODE_REASSERT_INTERVAL - timedelta(seconds=1)  # noqa: SLF001

    await zensdk_device.discharge(700)
    assert _last_props(zensdk_device) == {"outputLimit": 700, "inputLimit": 0, "smartMode": 1, "acMode": 2}


async def test_discharge_zero_power_sets_smart_mode_off(zensdk_device) -> None:  # noqa: ANN001
    await zensdk_device.discharge(0)

    assert _last_props(zensdk_device)["smartMode"] == 0


async def test_discharge_zero_power_keeps_smart_mode_on_when_offgrid(zensdk_device) -> None:  # noqa: ANN001
    zensdk_device.pwr_offgrid = 50
    await zensdk_device.discharge(0)

    assert _last_props(zensdk_device)["smartMode"] == 1


async def test_discharge_kickstart_boosts_power_from_stalled_start(zensdk_device) -> None:  # noqa: ANN001
    """When starting exactly at POWER_START with headroom and no home load, discharge() kickstarts above the noise floor."""
    zensdk_device.limitOutput.update_value(200)
    assert zensdk_device.homeOutput.asInt == 0  # default, no home load

    power = await zensdk_device.discharge(SmartMode.POWER_START)

    assert power == min(200 + 4, 2 * SmartMode.POWER_START)
    assert _last_props(zensdk_device)["outputLimit"] == power


async def test_charge_kickstart_boosts_power_from_stalled_start(zensdk_device) -> None:  # noqa: ANN001
    zensdk_device.limitInput.update_value(200)
    assert zensdk_device.homeInput.asInt == 0  # default, no home load

    power = await zensdk_device.charge(-SmartMode.POWER_START)

    assert power == -min(200 + 4, 2 * SmartMode.POWER_START)
    assert _last_props(zensdk_device)["inputLimit"] == -power


async def test_power_off_sends_zero_limits(zensdk_device) -> None:  # noqa: ANN001
    await zensdk_device.power_off()

    assert _last_props(zensdk_device) == {"outputLimit": 0, "inputLimit": 0, "smartMode": 0, "acMode": 2}


async def test_entity_write_output_limit_routes_through_discharge(zensdk_device) -> None:  # noqa: ANN001
    await zensdk_device.entityWrite(zensdk_device.limitOutput, 350)

    assert _last_props(zensdk_device) == {"outputLimit": 350, "inputLimit": 0, "smartMode": 1, "acMode": 2}


async def test_entity_write_input_limit_routes_through_charge(zensdk_device) -> None:  # noqa: ANN001
    await zensdk_device.entityWrite(zensdk_device.limitInput, 350)

    assert _last_props(zensdk_device) == {"outputLimit": 0, "inputLimit": 350, "smartMode": 1, "acMode": 1}


async def test_entity_write_alternating_automation_writes_do_not_flip_mode(zensdk_device) -> None:  # noqa: ANN001
    """Reproduces the #1521 report: an external automation repeatedly nudging one direction's
    limit must not make smartMode/acMode flip on every single write.
    """
    await zensdk_device.entityWrite(zensdk_device.limitInput, 300)
    await zensdk_device.entityWrite(zensdk_device.limitInput, 320)
    await zensdk_device.entityWrite(zensdk_device.limitInput, 280)

    mode_assertions = [c for c in zensdk_device.mqtt_commands if "smartMode" in c["properties"]]
    assert len(mode_assertions) == 1
