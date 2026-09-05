"""Scenario tests for ZendureZenSdk.charge / discharge / power_off."""

from __future__ import annotations

from custom_components.zendure_ha.const import SmartMode
from custom_components.zendure_ha.device import ZendureZenSdk


def _host_list(ip_address: str = "192.0.2.10", use_mdns: bool = True) -> list[str]:
    device = object.__new__(ZendureZenSdk)
    device.useMdns = use_mdns
    device.mdnsHost = "device.local"
    device.ipAddress = ip_address
    return device._http_hosts()


def test_http_hosts_prefers_manual_ip() -> None:
    assert _host_list("192.0.2.20", use_mdns=False) == ["192.0.2.20"]


def test_http_hosts_can_disable_mdns() -> None:
    assert _host_list(use_mdns=False) == ["192.0.2.10"]


def test_http_hosts_falls_back_from_mdns_to_cloud_ip() -> None:
    assert _host_list() == ["device.local", "192.0.2.10"]


def _last_props(device) -> dict:  # noqa: ANN001
    return device.mqtt_commands[-1]["properties"]


async def test_discharge_first_call_asserts_full_command(zensdk_device) -> None:  # noqa: ANN001
    await zensdk_device.discharge(500)

    assert len(zensdk_device.mqtt_commands) == 1
    assert _last_props(zensdk_device) == {"outputLimit": 500, "inputLimit": 0, "smartMode": 1, "acMode": 2}


async def test_charge_after_discharge_switches_mode(zensdk_device) -> None:  # noqa: ANN001
    """Switching direction sends acMode 1 and moves the power to inputLimit."""
    await zensdk_device.discharge(500)
    await zensdk_device.charge(-400)

    props = _last_props(zensdk_device)
    assert props == {"outputLimit": 0, "inputLimit": 400, "smartMode": 1, "acMode": 1}


async def test_discharge_zero_power_sets_smart_mode_off(zensdk_device) -> None:  # noqa: ANN001
    """ZendureZenSdk.pwr_offgrid is a fixed 0, so smartMode goes to 0 when power is 0."""
    await zensdk_device.discharge(0)

    assert _last_props(zensdk_device)["smartMode"] == 0


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
