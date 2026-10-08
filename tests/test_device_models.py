"""Tests for the productModel -> device class mapping."""

from __future__ import annotations

import pytest

from custom_components.zendure_ha.api import Api
from custom_components.zendure_ha.device import ZendureBattery
from custom_components.zendure_ha.devices.solarflow4000 import SolarFlow4000MixPro


def _lookup(product_model: str):  # noqa: ANN202
    """Resolve a productModel the same way ZendureManager does."""
    return Api.createdevice.get(product_model.lower().replace(" ", ""), None)


@pytest.mark.parametrize("product_model", ["SolarFlow 4000 Mix Pro", "solarFlow4000MixPro"])
def test_solarflow4000_mix_pro_is_supported(product_model: str) -> None:
    assert _lookup(product_model) is SolarFlow4000MixPro


async def test_solarflow4000_mix_pro_limits(hass) -> None:  # noqa: ANN001
    definition = {"productKey": "testProdKey", "snNumber": "EEE3TEST1", "productModel": "SolarFlow 4000 Mix Pro", "ip": ""}
    device = SolarFlow4000MixPro(hass, "device-1", "SolarFlow 4000 Mix Pro", definition)

    assert device.charge_limit == -4000
    assert device.discharge_limit == 4000
    assert device.maxSolar == -8000
    assert device.pwr_offgrid == 0


def test_solarflow4000_mix_pro_internal_pack() -> None:
    _, model, kwh = ZendureBattery.get_battery_type("BEAATEST1", 70)

    assert model == "I8000"
    assert kwh == 8.0
