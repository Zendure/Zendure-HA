from __future__ import annotations


class TestMqttCategoryLookup:
    """Verify _MQTT_CATEGORY maps props to the right category string."""

    def _category(self, prop: str) -> str:
        from custom_components.zendure_ha.device import ZendureZenSdk
        return ZendureZenSdk._MQTT_CATEGORY.get(prop, "number")

    def test_acmode_is_select(self) -> None:
        """'acMode' belongs to 'select' category."""
        assert self._category("acMode") == "select"

    def test_lampswitch_is_switch(self) -> None:
        """'lampSwitch' belongs to 'switch' category."""
        assert self._category("lampSwitch") == "switch"

    def test_outputlimit_is_number(self) -> None:
        """'outputLimit' belongs to 'number' category."""
        assert self._category("outputLimit") == "number"

    def test_unknown_prop_defaults_to_number(self) -> None:
        """An unknown property key must default to 'number'."""
        assert self._category("someUnknownProp") == "number"

    def test_gridreverse_is_select(self) -> None:
        """'gridReverse' belongs to 'select' category."""
        assert self._category("gridReverse") == "select"

    def test_smartmode_is_switch(self) -> None:
        """'smartMode' belongs to 'switch' category."""
        assert self._category("smartMode") == "switch"
