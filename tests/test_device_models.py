"""Tests for the productModel -> device class mapping."""

from __future__ import annotations

import pytest

from custom_components.zendure_ha.api import Api
from custom_components.zendure_ha.devices.solarflow4000 import SolarFlow4000AC_Plus


def _lookup(product_model: str):  # noqa: ANN202
    """Resolve a productModel the same way ZendureManager does."""
    return Api.createdevice.get(product_model.lower().replace(" ", ""), None)


@pytest.mark.parametrize(
    "product_model", ["SolarFlow 4000 AC+", "SolarFlow 4000 MIX AC+"]
)
def test_solarflow4000_models_are_supported(product_model: str) -> None:
    """The cloud reports the SF4000 Mix AC+ as 'SolarFlow 4000 MIX AC+'."""
    assert _lookup(product_model) is SolarFlow4000AC_Plus
