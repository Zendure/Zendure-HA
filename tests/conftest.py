"""Shared fixtures for the zendure_ha test suite."""

from __future__ import annotations

from typing import Any

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def _stub_add_entities_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entity classes queue themselves via a class-level `.add` callback normally
    supplied by async_add_entities during platform setup (see e.g. number.py's
    async_setup_entry). Tests construct devices directly without a platform, so
    stub `.add` out on every entity class that uses the pattern.
    """
    from custom_components.zendure_ha.binary_sensor import ZendureBinarySensor
    from custom_components.zendure_ha.button import ZendureButton
    from custom_components.zendure_ha.number import ZendureNumber
    from custom_components.zendure_ha.select import ZendureSelect
    from custom_components.zendure_ha.sensor import ZendureSensor
    from custom_components.zendure_ha.switch import ZendureSwitch

    def _noop(_entities: list[Any]) -> None:
        return

    for cls in (ZendureBinarySensor, ZendureButton, ZendureNumber, ZendureSelect, ZendureSensor, ZendureSwitch):
        monkeypatch.setattr(cls, "add", staticmethod(_noop), raising=False)


@pytest.fixture
def device_definition() -> dict[str, str]:
    """A minimal device 'definition' dict as passed by the coordinator/config entry."""
    return {
        "productKey": "testProdKey",
        "snNumber": "JO4ATEST1",
        "productModel": "SolarFlow 2400 AC",
        "ip": "",
    }


@pytest.fixture
async def zensdk_device(hass, device_definition):  # noqa: ANN001
    """A ZendureZenSdk device instance (e.g. SolarFlow 2400 AC) wired up for direct testing.

    Must be an async fixture: the constructor creates an aiohttp session via
    async_get_clientsession, which needs a running event loop. A plain sync
    fixture runs outside the loop pytest-asyncio set up for the `hass` fixture.
    """
    from custom_components.zendure_ha.device import ZendureZenSdk

    device = ZendureZenSdk(hass, "device-1", "SolarFlow 2400 AC", "SolarFlow 2400 AC", device_definition)
    # connection defaults to "cloud" (value 0), so doCommand() takes the
    # mqttPublish path (recorded below) rather than an HTTP POST.
    device.mqtt_commands = []
    device.mqttPublish = lambda topic, command, mqtt=None: device.mqtt_commands.append(command)  # noqa: ARG005
    return device
