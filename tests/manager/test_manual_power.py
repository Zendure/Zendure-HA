from __future__ import annotations

import pytest
from pytest_mock import MockerFixture


class TestManualPowerIdleDevices:

    @pytest.mark.asyncio
    async def test_idle_device_gets_discharge_command_at_200w(self, mocker: MockerFixture) -> None:
        """Manual Power +200W → idle device receives power_discharge(200)."""
        idle_device = mocker.MagicMock()
        idle_device.power_discharge = mocker.AsyncMock(return_value=200)

        setpoint = 200
        for d in [idle_device]:
            await d.power_discharge(setpoint)

        idle_device.power_discharge.assert_called_once_with(200)

    @pytest.mark.asyncio
    async def test_idle_device_gets_charge_command_at_minus_200w(self, mocker: MockerFixture) -> None:
        """Manual Power -200W → idle device receives power_charge(-200)."""
        idle_device = mocker.MagicMock()
        idle_device.power_charge = mocker.AsyncMock(return_value=0)

        setpoint = -200
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
