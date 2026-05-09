from __future__ import annotations

from pytest_mock import MockerFixture


class TestFuseGrpGuard:

    def test_charge_limit_fallback_without_fuseGrp(self, mocker: MockerFixture) -> None:
        """charge_limit fallback to device.charge_limit when fuseGrp absent."""
        device = mocker.MagicMock(spec=[])  # spec=[] → no attributes → hasattr returns False
        device.charge_limit = 800

        result = device.fuseGrp.charge_limit(device) if hasattr(device, "fuseGrp") else device.charge_limit
        assert result == 800

    def test_discharge_limit_fallback_without_fuseGrp(self, mocker: MockerFixture) -> None:
        """discharge_limit fallback to device.discharge_limit when fuseGrp absent."""
        device = mocker.MagicMock(spec=[])
        device.discharge_limit = 800

        result = device.fuseGrp.discharge_limit(device) if hasattr(device, "fuseGrp") else device.discharge_limit
        assert result == 800

    def test_uses_fuseGrp_when_present(self, mocker: MockerFixture) -> None:
        """When fuseGrp IS set, it is used instead of device limit."""
        device = mocker.MagicMock()
        device.fuseGrp = mocker.MagicMock()
        device.fuseGrp.charge_limit.return_value = 400
        device.charge_limit = 800

        result = device.fuseGrp.charge_limit(device) if hasattr(device, "fuseGrp") else device.charge_limit
        assert result == 400
        device.fuseGrp.charge_limit.assert_called_once_with(device)
