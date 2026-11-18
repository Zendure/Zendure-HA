"""ZendureManager must keep polling after a refresh raises (#1781).

None of the integration's entities are CoordinatorEntity, so the manager's
DataUpdateCoordinator has no listeners - and DataUpdateCoordinator only
reschedules itself in `_async_refresh`'s `finally` when listeners exist. The
manager therefore schedules its next poll by hand at the end of
`_async_update_data`. When anything before that line raised, the reschedule was
skipped and polling stopped for good: every sensor froze until the integration
was reloaded, while the device itself stayed reachable.

These tests drive a real ZendureManager through the real coordinator refresh
and timer (async_fire_time_changed), with a stand-in device whose refresh fails.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from custom_components.zendure_ha.const import DOMAIN
from custom_components.zendure_ha.manager import SCAN_INTERVAL, ZendureManager


class _FlakyDevice:
    """Stand-in exposing only what _async_update_data touches; the first `failures` refreshes raise."""

    def __init__(self, failures: int) -> None:
        self.name = "SolarFlow 2400 Pro"
        self.deviceId = "device-1"
        self.kWh = 2.88
        self.hemsState = SimpleNamespace(is_on=False)
        self.failures = failures
        self.refreshes = 0

    async def dataRefresh(self, _update_count: int) -> None:
        self.refreshes += 1
        if self.refreshes <= self.failures:
            msg = "simulated refresh failure"
            raise RuntimeError(msg)

    def setStatus(self) -> None:
        return


class _RecordingSensor:
    def __init__(self) -> None:
        self.values: list[Any] = []

    def update_value(self, value: Any) -> None:
        self.values.append(value)


@pytest.fixture
async def manager(hass: HomeAssistant) -> ZendureManager:
    """Create a real ZendureManager without loadDevices(), which needs the Zendure cloud."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    manager = ZendureManager(hass, entry)
    # Normally created by loadDevices(); `devices` is a class attribute, so shadow it per instance.
    manager.totalKwh = _RecordingSensor()
    manager.devices = []
    yield manager
    await manager.async_shutdown()


async def _next_poll(hass: HomeAssistant, polls: int) -> None:
    """Advance time past the given number of scan intervals and let the refresh run."""
    async_fire_time_changed(hass, dt_util.utcnow() + polls * (SCAN_INTERVAL + timedelta(seconds=1)))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_polling_continues_after_a_failed_refresh(hass: HomeAssistant, manager: ZendureManager, caplog: pytest.LogCaptureFixture) -> None:
    device = _FlakyDevice(failures=1)
    manager.devices = [device]

    await manager.async_refresh()

    assert device.refreshes == 1
    # The error is not swallowed: HA still logs it and marks the update as failed.
    assert manager.last_update_success is False
    assert "Unexpected error fetching Zendure Manager data" in caplog.text

    await _next_poll(hass, 1)

    assert device.refreshes == 2, "no poll after the failed refresh - polling stopped for good"
    assert manager.last_update_success is True

    await _next_poll(hass, 2)

    assert device.refreshes == 3


async def test_polling_keeps_retrying_while_refreshes_keep_failing(hass: HomeAssistant, manager: ZendureManager) -> None:
    device = _FlakyDevice(failures=3)
    manager.devices = [device]

    await manager.async_refresh()
    for poll in range(1, 4):
        await _next_poll(hass, poll)

    assert device.refreshes == 4
    assert manager.last_update_success is True


async def test_shutdown_cancels_the_poll_scheduled_after_a_failed_refresh(hass: HomeAssistant, manager: ZendureManager) -> None:
    """Do not leave an orphaned poller behind when setup fails.

    A failed first refresh during setup raises ConfigEntryNotReady; HA then runs the
    entry's on_unload callbacks, which include the coordinator's async_shutdown. The
    poll the failed refresh scheduled must not survive that.
    """
    device = _FlakyDevice(failures=1)
    manager.devices = [device]

    await manager.async_refresh()
    await manager.async_shutdown()
    await _next_poll(hass, 1)

    assert device.refreshes == 1
