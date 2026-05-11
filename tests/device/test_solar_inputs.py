from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pytest_mock import MockerFixture

from tests.device.helpers import _patch_entity_add


class TestSolarFlow800Pro2Inputs:
    """Verify SolarFlow800Pro2 adds solarPower3/solarPower4; Pro does not have them."""

    def _make_device(self, mocker: MockerFixture, cls: Any) -> Any:
        """Instantiate cls with a minimal mock hass and definition dict.

        Patches entity `add` class attributes so constructors don't raise
        AttributeError when calling self.add([self]).
        """
        _patch_entity_add(mocker)
        hass = mocker.MagicMock()
        hass.loop = asyncio.new_event_loop()
        definition = {
            "productKey": "testKey",
            "productModel": "solarFlow800Pro2",
            "snNumber": "TEST12345",
            "ip": "",
        }
        return cls(hass, "TESTDEV", "SolarFlow800Pro2", definition)

    def test_pro2_has_solarpower3(self, mocker: MockerFixture) -> None:
        """SolarFlow800Pro2 instance must expose solarPower3 attribute."""
        from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro2
        device = self._make_device(mocker, SolarFlow800Pro2)
        assert hasattr(device, "solarPower3")

    def test_pro2_has_solarpower4(self, mocker: MockerFixture) -> None:
        """SolarFlow800Pro2 instance must expose solarPower4 attribute."""
        from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro2
        device = self._make_device(mocker, SolarFlow800Pro2)
        assert hasattr(device, "solarPower4")

    def test_pro_does_not_have_solarpower3(self, mocker: MockerFixture) -> None:
        """SolarFlow800Pro (not Pro2) must NOT have solarPower3."""
        from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro
        device = self._make_device(mocker, SolarFlow800Pro)
        assert not hasattr(device, "solarPower3")

    def test_pro_does_not_have_solarpower4(self, mocker: MockerFixture) -> None:
        """SolarFlow800Pro (not Pro2) must NOT have solarPower4."""
        from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro
        device = self._make_device(mocker, SolarFlow800Pro)
        assert not hasattr(device, "solarPower4")

    def test_pro2_inherits_from_pro(self) -> None:
        """SolarFlow800Pro2 must be a subclass of SolarFlow800Pro."""
        from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro, SolarFlow800Pro2
        assert issubclass(SolarFlow800Pro2, SolarFlow800Pro)
