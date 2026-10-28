from __future__ import annotations

import pytest

from custom_components.zendure_ha.manager import ZendureManager


@pytest.fixture(autouse=True)
def balancing_off() -> None:
    old = ZendureManager.soc_balancing
    ZendureManager.soc_balancing = False
    yield
    ZendureManager.soc_balancing = old
