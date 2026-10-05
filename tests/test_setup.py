"""The integration sets up from a v2 entry, exposes the fleet entities and unloads cleanly."""

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zendure_ha.const import DOMAIN
from custom_components.zendure_ha.zendure.driver import ZendureDriver
from helpers import subentry


@pytest.fixture(autouse=True)
def _offline(hass, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    async def connect(self) -> None:  # noqa: ANN001
        return

    monkeypatch.setattr(ZendureDriver, "connect", connect)
    for dep in ("recorder", "bluetooth", "zeroconf"):
        hass.config.components.add(dep)


async def test_setup_and_unload(hass) -> None:  # noqa: ANN001
    entry = MockConfigEntry(domain=DOMAIN, title="Zendure", unique_id="Zendure", version=2, data={"distribution": {"max_kw": 12.0, "mqtt_log": False}, "zendure": {}}, subentries_data=[subentry({"product": "solarFlow800Pro", "sn": "WOB1NHMAEA88", "device_id": "9RUB1mW7", "prodkey": "R3mn8U", "ip": "", "blemac": ""}, "SF800_88")])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    assert hass.states.get("select.zendure_mode") is not None
    assert hass.states.get("number.zendure_manual_power") is not None
    assert hass.states.get("sensor.zendure_available_kwh") is not None
    assert hass.states.get("sensor.sf800_88_home_output") is not None
    assert hass.states.get("number.sf800_88_soc_min") is not None
    assert hass.states.get("binary_sensor.sf800_88_bypass") is not None
    assert hass.states.get("sensor.sf800_88_home_output_aggr") is not None

    coordinator = hass.data[DOMAIN][entry.entry_id]
    device = coordinator.zendure.devices["WOB1NHMAEA88"]
    device.update_properties({"properties": {"electricLevel": 42, "outputHomePower": 300, "gridInputPower": 0}}, 0.0, True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.sf800_88_soc").state == "42"
    assert hass.states.get("sensor.sf800_88_home_output").state == "300"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
