from __future__ import annotations

import pytest


class TestCreateDeviceMapping:

    def test_solarflow800pro2_mapped(self) -> None:
        """'solarflow800pro2' (lowercase) maps to SolarFlow800Pro2."""
        from custom_components.zendure_ha.api import Api
        from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro2

        assert Api.createdevice.get("solarflow800pro2") is SolarFlow800Pro2

    def test_manager_lookup_is_case_insensitive(self) -> None:
        """manager.py does .lower().strip() — camelCase product name from device must resolve."""
        from custom_components.zendure_ha.api import Api
        from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro2

        assert Api.createdevice.get("solarflow800pro2".lower().strip()) is SolarFlow800Pro2

    @pytest.mark.parametrize("key", [
        "hub 1200", "hub 2000", "hyper 2000", "solarflow 800", "solarflow 800 pro",
    ])
    def test_existing_models_not_removed(self, key: str) -> None:
        """Regression: existing model keys still present after our patch."""
        from custom_components.zendure_ha.api import Api

        assert key in Api.createdevice
