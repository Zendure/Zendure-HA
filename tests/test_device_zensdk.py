"""Scenario tests for ZendureZenSdk.charge / discharge / power_off."""

from __future__ import annotations

from custom_components.zendure_ha.const import SmartMode


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


def _stub_http(device, monkeypatch, *, post_ok: bool = True, report: dict | None = None) -> list:  # noqa: ANN001
    """Record HTTP calls and skip the post-write delay."""
    calls: list = []

    async def http_post(url: str, command: dict) -> bool:
        calls.append(("POST", url, command))
        return post_ok

    async def http_get(url: str, _key: str | None = None) -> dict:
        calls.append(("GET", url))
        return report or {}

    async def no_sleep(_delay: float) -> None:
        return

    device.httpPost = http_post
    device.httpGet = http_get
    monkeypatch.setattr("custom_components.zendure_ha.device.asyncio.sleep", no_sleep)
    return calls


async def test_zensdk_write_reads_back_immediately(zensdk_device, monkeypatch) -> None:  # noqa: ANN001
    """After a successful HTTP write the device is read back instead of waiting for the next refresh."""
    zensdk_device.connection.update_value(SmartMode.ZENSDK)
    report = {"properties": {"outputLimit": 300}}
    calls = _stub_http(zensdk_device, monkeypatch, report=report)
    received: list = []

    async def mqtt_properties(payload: dict) -> None:
        received.append(payload)

    zensdk_device.mqttProperties = mqtt_properties

    await zensdk_device.entityWrite(zensdk_device.limitOutput, 300)

    assert calls == [("POST", "properties/write", {"properties": {"outputLimit": 300}}), ("GET", "properties/report")]
    assert received == [report]


async def test_zensdk_failed_write_skips_read_back(zensdk_device, monkeypatch) -> None:  # noqa: ANN001
    zensdk_device.connection.update_value(SmartMode.ZENSDK)
    calls = _stub_http(zensdk_device, monkeypatch, post_ok=False)

    await zensdk_device.entityWrite(zensdk_device.limitOutput, 300)

    assert [c[0] for c in calls] == ["POST"]


async def test_cloud_write_requests_get_all(zensdk_device, monkeypatch) -> None:  # noqa: ANN001
    """Over MQTT the write is followed by a getAll read request."""
    monkeypatch.setattr(type(zensdk_device), "online", property(lambda _self: True))

    await zensdk_device.entityWrite(zensdk_device.limitOutput, 300)

    assert zensdk_device.mqtt_commands[-1]["properties"] == ["getAll"]
