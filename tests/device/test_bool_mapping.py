from __future__ import annotations


class TestZenSdkBoolMapping:
    """Verify every entry in _ZENSDK_BOOL resolves to the correct integer."""

    def _mapping(self):
        from custom_components.zendure_ha.api import _ZENSDK_BOOL
        return _ZENSDK_BOOL

    def test_on_maps_to_1(self) -> None:
        """'ON' must map to integer 1."""
        assert self._mapping()["ON"] == 1

    def test_off_maps_to_0(self) -> None:
        """'OFF' must map to integer 0."""
        assert self._mapping()["OFF"] == 0

    def test_yes_maps_to_1(self) -> None:
        """'yes' must map to integer 1."""
        assert self._mapping()["yes"] == 1

    def test_no_maps_to_0(self) -> None:
        """'no' must map to integer 0."""
        assert self._mapping()["no"] == 0

    def test_not_heating_maps_to_0(self) -> None:
        """'not_heating' must map to integer 0."""
        assert self._mapping()["not_heating"] == 0

    def test_heating_maps_to_1(self) -> None:
        """'heating' must map to integer 1."""
        assert self._mapping()["heating"] == 1
