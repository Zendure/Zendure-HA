from __future__ import annotations

import pytest
from pytest_mock import MockerFixture


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

        status_holder = {"value": None}
        device.connectionStatus.update_value.side_effect = lambda v: status_holder.update({"value": v})
        device.connectionStatus.asInt = 0

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
