"""Tests for the "last message received" diagnostic sensor (#1785).

The regular entities only write a new state when the value changed, so `last_reported`
(and `last_updated`) of a power sensor shows the last *change*, not the last *message*.
A device that keeps sending identical values looks silent. The `last_received` sensor is
stamped when a message arrives, before those value filters, and is throttled so that it
does not add a database row for every message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import async_capture_events

from custom_components.zendure_ha.device import LAST_RECEIVED_INTERVAL, ZendureLegacy, ZendureZenSdk
from custom_components.zendure_ha.sensor import ZendureSensor

TRANSLATIONS = Path(__file__).parent.parent / "custom_components" / "zendure_ha" / "translations"

START = "2026-11-25 12:00:00+00:00"
SENSOR_ID = "sensor.test_device_last_received"
LEVEL_ID = "sensor.test_device_electric_level"


@pytest.fixture(autouse=True)
def _frozen_clock(freezer: Any) -> None:
    """Freeze time on a whole second, because Home Assistant drops microseconds from a timestamp state."""
    freezer.move_to(START)


@pytest.fixture
def sensor_platform(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> EntityPlatform:
    """Route ZendureSensor.add through a real EntityPlatform, as sensor.async_setup_entry does."""
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="sensor",
        platform_name="zendure_ha",
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    monkeypatch.setattr(ZendureSensor, "add", staticmethod(platform._async_schedule_add_entities))  # noqa: SLF001
    return platform


@pytest.fixture
async def legacy_device(hass: HomeAssistant, sensor_platform: EntityPlatform, device_definition: dict[str, str]) -> ZendureLegacy:  # noqa: ARG001
    """Create a cloud/local MQTT device whose sensors are registered with Home Assistant."""
    device = ZendureLegacy(hass, "device-1", "Test Device", "SolarFlow 2400 AC", device_definition)
    await hass.async_block_till_done()
    return device


@pytest.fixture
async def zensdk_platform_device(hass: HomeAssistant, sensor_platform: EntityPlatform, device_definition: dict[str, str]) -> ZendureZenSdk:  # noqa: ARG001
    """Create a zenSDK (HTTP) device whose sensors are registered with Home Assistant."""
    device = ZendureZenSdk(hass, "device-1", "Test Device", "SolarFlow 2400 AC", device_definition)
    await hass.async_block_till_done()
    return device


def _answer_http(device: ZendureZenSdk, payload: dict[str, Any]) -> None:
    """Make the device's HTTP session answer every GET with the given JSON payload."""

    class _Response:
        async def text(self) -> str:
            return json.dumps(payload)

    async def _get(*_args: Any, **_kwargs: Any) -> _Response:
        return _Response()

    device.session.get = _get


def _sensor_writes(events: list[Event]) -> list[Event]:
    return [e for e in events if e.data["entity_id"] == SENSOR_ID]


async def test_message_sets_timestamp_and_sensor_state(hass: HomeAssistant, legacy_device: ZendureLegacy) -> None:
    assert hass.states.get(SENSOR_ID).state == "unknown"
    assert legacy_device.last_received is None

    now = dt_util.utcnow()
    await legacy_device.mqttProperties({"properties": {"electricLevel": 50}})
    await hass.async_block_till_done()

    assert legacy_device.last_received == now
    assert hass.states.get(SENSOR_ID).state == now.isoformat()


async def test_timestamp_advances_when_every_value_is_unchanged(hass: HomeAssistant, legacy_device: ZendureLegacy, freezer: Any) -> None:
    """The value filters drop unchanged values; the timestamp must not depend on them."""
    await legacy_device.mqttProperties({"properties": {"electricLevel": 50}})
    await hass.async_block_till_done()
    level_changed = hass.states.get(LEVEL_ID).last_updated

    freezer.tick(timedelta(seconds=120))
    later = dt_util.utcnow()
    await legacy_device.mqttProperties({"properties": {"electricLevel": 50}})
    await hass.async_block_till_done()

    # The unchanged value was filtered (the value sensor was not rewritten) ...
    assert hass.states.get(LEVEL_ID).last_updated == level_changed
    # ... but the receive time still moved on.
    assert legacy_device.last_received == later
    assert hass.states.get(SENSOR_ID).state == later.isoformat()


@pytest.mark.parametrize("payload", [{}, {"properties": {}}, {"properties": None}, {"packData": []}, {"messageId": 1, "timestamp": 1}])
async def test_message_without_data_does_not_renew_timestamp(hass: HomeAssistant, legacy_device: ZendureLegacy, freezer: Any, payload: dict[str, Any]) -> None:
    await legacy_device.mqttProperties({"properties": {"electricLevel": 50}})
    await hass.async_block_till_done()
    first = legacy_device.last_received

    freezer.tick(timedelta(seconds=120))
    await legacy_device.mqttProperties(payload)
    await hass.async_block_till_done()

    assert legacy_device.last_received == first
    assert hass.states.get(SENSOR_ID).state == first.isoformat()


async def test_empty_message_before_any_data_leaves_sensor_unknown(hass: HomeAssistant, legacy_device: ZendureLegacy) -> None:
    await legacy_device.mqttProperties({})
    await hass.async_block_till_done()

    assert legacy_device.last_received is None
    assert hass.states.get(SENSOR_ID).state == "unknown"


async def test_battery_pack_message_counts_as_received(hass: HomeAssistant, legacy_device: ZendureLegacy) -> None:
    now = dt_util.utcnow()
    await legacy_device.mqttProperties({"packData": [{"sn": "CO4ATEST1", "socLevel": 40}]})
    await hass.async_block_till_done()

    assert legacy_device.last_received == now


async def test_sensor_state_is_written_at_most_once_per_interval(hass: HomeAssistant, legacy_device: ZendureLegacy, freezer: Any) -> None:
    events = async_capture_events(hass, "state_changed")
    payload = {"properties": {"electricLevel": 50}}

    start = dt_util.utcnow()
    await legacy_device.mqttProperties(payload)  # t = 0: written
    await hass.async_block_till_done()
    assert len(_sensor_writes(events)) == 1

    # A message every second for just under the interval: no further writes ...
    for _ in range(int(LAST_RECEIVED_INTERVAL.total_seconds()) - 1):
        freezer.tick(timedelta(seconds=1))
        await legacy_device.mqttProperties(payload)
    await hass.async_block_till_done()
    assert len(_sensor_writes(events)) == 1
    assert hass.states.get(SENSOR_ID).state == start.isoformat()
    # ... while the exact receive time is still tracked on the device.
    assert legacy_device.last_received == start + LAST_RECEIVED_INTERVAL - timedelta(seconds=1)

    # Once the interval has passed the next message is written again.
    freezer.tick(timedelta(seconds=1))
    await legacy_device.mqttProperties(payload)
    await hass.async_block_till_done()
    assert len(_sensor_writes(events)) == 2
    assert hass.states.get(SENSOR_ID).state == (start + LAST_RECEIVED_INTERVAL).isoformat()


async def test_clock_moving_backwards_does_not_freeze_the_sensor(hass: HomeAssistant, legacy_device: ZendureLegacy, freezer: Any) -> None:
    payload = {"properties": {"electricLevel": 50}}
    await legacy_device.mqttProperties(payload)
    await hass.async_block_till_done()

    freezer.tick(timedelta(hours=-1))
    earlier = dt_util.utcnow()
    await legacy_device.mqttProperties(payload)
    await hass.async_block_till_done()

    assert hass.states.get(SENSOR_ID).state == earlier.isoformat()


async def _poll(device: ZendureZenSdk, path: str) -> None:
    """Run one zenSDK poll the way the integration does it."""
    device.connection.update_value(2)  # zenSDK
    if path == "dataRefresh":
        await device.dataRefresh(1)
    else:
        await device.power_get()


def _fail_http(device: ZendureZenSdk) -> None:
    async def _get(*_args: Any, **_kwargs: Any) -> None:
        raise TimeoutError

    device.session.get = _get


@pytest.mark.parametrize("path", ["dataRefresh", "power_get"])
async def test_zensdk_poll_with_properties_sets_timestamp_once(hass: HomeAssistant, zensdk_platform_device: ZendureZenSdk, path: str) -> None:
    device = zensdk_platform_device
    events = async_capture_events(hass, "state_changed")
    _answer_http(device, {"properties": {"electricLevel": 50}})
    now = dt_util.utcnow()

    await _poll(device, path)
    await hass.async_block_till_done()

    assert device.last_received == now
    assert hass.states.get(SENSOR_ID).state == now.isoformat()
    # httpGet itself does not stamp and the reply is handed to mqttProperties only once.
    assert len(_sensor_writes(events)) == 1


@pytest.mark.parametrize("path", ["dataRefresh", "power_get"])
@pytest.mark.parametrize("reply", ["empty", "error", "timeout"])
async def test_zensdk_poll_without_data_does_not_renew_timestamp(
    hass: HomeAssistant, zensdk_platform_device: ZendureZenSdk, freezer: Any, path: str, reply: str
) -> None:
    device = zensdk_platform_device
    first = dt_util.utcnow()
    device.markReceived()
    await hass.async_block_till_done()

    match reply:
        case "empty":
            _answer_http(device, {})
        case "error":
            _answer_http(device, {"code": 500, "msg": "error"})
        case "timeout":
            _fail_http(device)
    freezer.tick(timedelta(seconds=120))

    await _poll(device, path)
    await hass.async_block_till_done()

    assert device.last_received == first
    assert hass.states.get(SENSOR_ID).state == first.isoformat()


async def test_zensdk_poll_with_unchanged_values_still_renews_timestamp(hass: HomeAssistant, zensdk_platform_device: ZendureZenSdk, freezer: Any) -> None:
    """The reported use case: a zenSDK device polled while its values stay the same."""
    device = zensdk_platform_device

    _answer_http(device, {"properties": {"electricLevel": 50}})
    await _poll(device, "dataRefresh")
    await hass.async_block_till_done()

    freezer.tick(timedelta(seconds=120))
    later = dt_util.utcnow()
    await _poll(device, "dataRefresh")
    await hass.async_block_till_done()

    assert device.last_received == later
    assert hass.states.get(SENSOR_ID).state == later.isoformat()


async def test_message_before_the_entity_is_registered_does_not_start_the_throttle(hass: HomeAssistant, device_definition: dict[str, str], freezer: Any) -> None:
    """A write the entity ignored must not block the next one for the whole interval."""
    device = ZendureLegacy(hass, "device-1", "Test Device", "SolarFlow 2400 AC", device_definition)  # sensors not registered with HA yet
    await device.mqttProperties({"properties": {"electricLevel": 50}})
    assert hass.states.get(SENSOR_ID) is None

    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="sensor",
        platform_name="zendure_ha",
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform._async_schedule_add_entities([device.lastReceived])  # noqa: SLF001
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR_ID).state == "unknown"

    freezer.tick(timedelta(seconds=5))  # well inside the throttle interval
    later = dt_util.utcnow()
    await device.mqttProperties({"properties": {"electricLevel": 50}})
    await hass.async_block_till_done()

    assert hass.states.get(SENSOR_ID).state == later.isoformat()


async def test_message_from_the_mqtt_thread_is_stamped_in_the_event_loop(hass: HomeAssistant, legacy_device: ZendureLegacy) -> None:
    """The MQTT client calls mqttMessage from its own thread; the state write must happen in the loop."""
    loop_thread = threading.get_ident()
    stamped_in: list[int] = []
    original = legacy_device.markReceived

    def _spy() -> None:
        stamped_in.append(threading.get_ident())
        original()

    legacy_device.markReceived = _spy
    caller: list[int] = []

    def _from_mqtt_thread() -> bool:
        caller.append(threading.get_ident())
        return legacy_device.mqttMessage("properties/report", {"properties": {"electricLevel": 50}})

    assert await hass.async_add_executor_job(_from_mqtt_thread)
    # run_coroutine_threadsafe starts a plain asyncio task that block_till_done does not track.
    for _ in range(10):
        await asyncio.sleep(0)
    await hass.async_block_till_done()

    assert caller[0] != loop_thread
    assert stamped_in == [loop_thread]
    assert hass.states.get(SENSOR_ID).state == dt_util.utcnow().isoformat()


async def test_sensor_is_a_diagnostic_timestamp(legacy_device: ZendureLegacy) -> None:
    sensor = legacy_device.lastReceived

    assert sensor.device_class == "timestamp"
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.translation_key == "last_received"
    assert sensor.native_unit_of_measurement is None
    assert sensor.state_class is None


@pytest.mark.parametrize("language", sorted(p.stem for p in TRANSLATIONS.glob("*.json")))
def test_sensor_name_is_translated_in_every_language(language: str) -> None:
    translations = json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))

    assert translations["entity"]["sensor"]["last_received"]["name"]
