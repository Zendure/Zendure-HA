"""Smoke test: the real ZendureManager must import under the Home Assistant test stack."""


def test_real_manager_imports():
    from custom_components.zendure_ha.const import ManagerMode
    from custom_components.zendure_ha.manager import ZendureManager

    assert ManagerMode.OFF.value == 0
    # the real distribution methods we intend to exercise exist on the real class
    assert callable(ZendureManager.powerChanged)
    assert callable(ZendureManager.power_discharge)
