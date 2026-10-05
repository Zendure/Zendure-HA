"""Shared fixtures for the zendure_ha test suite."""

from __future__ import annotations

import sys

import pytest
import pytest_socket
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zendure_ha.const import DOMAIN

pytest_plugins = "pytest_homeassistant_custom_component"

if sys.platform == "win32":
    # asyncio on Windows builds its self-pipe from an AF_INET socketpair; pytest_socket only lets AF_UNIX through
    pytest_socket._is_unix_socket = lambda family: True


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    return


@pytest.fixture(autouse=True)
def _stub_add_entities_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    # the platforms hand every entity class its async_add_entities; tests build devices without a platform
    from custom_components.zendure_ha.binary_sensor import HaBinarySensor
    from custom_components.zendure_ha.button import HaButton
    from custom_components.zendure_ha.number import HaNumber
    from custom_components.zendure_ha.select import HaSelect
    from custom_components.zendure_ha.sensor import HaSensor
    from custom_components.zendure_ha.switch import HaSwitch
    from custom_components.zendure_ha.time import HaTime

    for cls in (HaBinarySensor, HaButton, HaNumber, HaSelect, HaSensor, HaSwitch, HaTime):
        monkeypatch.setattr(cls, "add", staticmethod(lambda *args, **kwargs: None), raising=False)


@pytest.fixture
def entry(hass) -> MockConfigEntry:  # noqa: ANN001
    # a v2 entry without devices; tests add subentries as they need them
    entry = MockConfigEntry(domain=DOMAIN, title="Zendure", unique_id="Zendure", version=2, data={"distribution": {"max_kw": 17.0, "mqtt_log": False}, "zendure": {}})
    entry.add_to_hass(hass)
    return entry
