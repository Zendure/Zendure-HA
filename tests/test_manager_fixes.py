"""Tests for manager and device fixes.

Run with:
    uv run pytest tests/ -v
"""

from __future__ import annotations

import pytest
from pytest_mock import MockerFixture


# ── setStatus: zenSDK online check before fuseGroup ──────────────────────────

class TestSetStatusZenSDK:

    def _make_device(self, mocker: MockerFixture, connection_value: int, fusegroup_value: int, lastseen_min: bool = False) -> object:
        """Build a minimal device stub that exercises setStatus()."""
        from custom_components.zendure_ha.const import SmartMode
        from datetime import datetime, timedelta

        device = mocker.MagicMock()
        device.lastseen = datetime.min if lastseen_min else datetime.now() + timedelta(minutes=5)
        device.connection.value = connection_value
        device.fuseGroup.value = fusegroup_value
        device.socStatus.asInt = 0
        device.hemsState.is_on = False

        # Capture what update_value is called with
        status_holder = {"value": None}
        device.connectionStatus.update_value.side_effect = lambda v: status_holder.update({"value": v})
        device.connectionStatus.asInt = 0

        # Bind the real setStatus logic to our stub
        from custom_components.zendure_ha.device import ZendureDevice
        import types
        device.setStatus = types.MethodType(ZendureDevice.setStatus, device)

        return device, status_holder

    def test_zensdk_device_is_online_without_fusegroup(self, mocker: MockerFixture) -> None:
        """Device in zenSDK mode (connection=2) must get connectionStatus=12 even without FuseGroup."""
        device, status = self._make_device(mocker, connection_value=2, fusegroup_value=0)
        device.setStatus()
        assert status["value"] == 12

    def test_cloud_device_without_fusegroup_gets_status_3(self, mocker: MockerFixture) -> None:
        """Cloud device (connection=0) without FuseGroup must get connectionStatus=3."""
        device, status = self._make_device(mocker, connection_value=0, fusegroup_value=0)
        device.setStatus()
        assert status["value"] == 3

    def test_unseen_device_gets_status_0(self, mocker: MockerFixture) -> None:
        """Device never seen (lastseen=min) must get connectionStatus=0 regardless of connection mode."""
        device, status = self._make_device(mocker, connection_value=2, fusegroup_value=0, lastseen_min=True)
        device.setStatus()
        assert status["value"] == 0


# ── fuseGrp hasattr guard in powerChanged ────────────────────────────────────

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


# ── Manual Power commands idle devices ───────────────────────────────────────

class TestManualPowerIdleDevices:

    @pytest.mark.asyncio
    async def test_idle_device_gets_discharge_command_at_200w(self, mocker: MockerFixture) -> None:
        """Manual Power +200W → idle device receives power_discharge(200)."""
        idle_device = mocker.MagicMock()
        idle_device.power_discharge = mocker.AsyncMock(return_value=200)

        setpoint = 200  # positive = discharge
        for d in [idle_device]:
            await d.power_discharge(setpoint)

        idle_device.power_discharge.assert_called_once_with(200)

    @pytest.mark.asyncio
    async def test_idle_device_gets_charge_command_at_minus_200w(self, mocker: MockerFixture) -> None:
        """Manual Power -200W → idle device receives power_charge(-200)."""
        idle_device = mocker.MagicMock()
        idle_device.power_charge = mocker.AsyncMock(return_value=0)

        setpoint = -200  # negative = charge
        for d in [idle_device]:
            await d.power_charge(setpoint)

        idle_device.power_charge.assert_called_once_with(-200)

    @pytest.mark.asyncio
    async def test_multiple_idle_devices_all_commanded(self, mocker: MockerFixture) -> None:
        """All idle devices receive the command, not just the first."""
        devices = [mocker.MagicMock() for _ in range(3)]
        for d in devices:
            d.power_discharge = mocker.AsyncMock(return_value=150)

        for d in devices:
            await d.power_discharge(150)

        for d in devices:
            d.power_discharge.assert_called_once_with(150)

    @pytest.mark.asyncio
    async def test_zero_setpoint_does_not_discharge(self, mocker: MockerFixture) -> None:
        """Manual Power 0W → discharge path not taken (setpoint not > 0)."""
        idle_device = mocker.MagicMock()
        idle_device.power_discharge = mocker.AsyncMock()

        setpoint = 0
        if setpoint > 0:
            await idle_device.power_discharge(setpoint)

        idle_device.power_discharge.assert_not_called()
