from __future__ import annotations

import pytest
from pytest_mock import MockerFixture

from tests.device.helpers import _make_refresh_device


class TestDataRefresh:
    """Test that dataRefresh skips HTTP polling when MQTT push is active."""

    @pytest.mark.asyncio
    async def test_mqtt_connected_skips_httpget(self, mocker: MockerFixture) -> None:
        """When mqtt is set, dataRefresh returns immediately without calling httpGet."""
        device = _make_refresh_device(mocker, connection_value=2, has_mqtt=True)
        await device.dataRefresh(0)
        device.httpGet.assert_not_called()

    @pytest.mark.asyncio
    async def test_zensdk_no_mqtt_calls_httpget(self, mocker: MockerFixture) -> None:
        """zenSDK mode (connection=2) with mqtt=None → httpGet must be called."""
        device = _make_refresh_device(mocker, connection_value=2, has_mqtt=False)
        await device.dataRefresh(0)
        device.httpGet.assert_called_once_with("properties/report")

    @pytest.mark.asyncio
    async def test_cloud_update0_offline_calls_httpget(self, mocker: MockerFixture) -> None:
        """Cloud mode, update_count=0, offline → fallback httpGet is called."""
        device = _make_refresh_device(mocker, connection_value=0, has_mqtt=False, online=False)
        await device.dataRefresh(0)
        device.httpGet.assert_called_once_with("properties/report")

    @pytest.mark.asyncio
    async def test_cloud_update1_does_not_call_httpget(self, mocker: MockerFixture) -> None:
        """Cloud mode, update_count=1 → neither condition is met, httpGet not called."""
        device = _make_refresh_device(mocker, connection_value=0, has_mqtt=False, online=True)
        await device.dataRefresh(1)
        device.httpGet.assert_not_called()

    @pytest.mark.asyncio
    async def test_cloud_update0_online_does_not_call_httpget(self, mocker: MockerFixture) -> None:
        """Cloud mode, update_count=0 but device is online → httpGet not called (fallback only for offline)."""
        device = _make_refresh_device(mocker, connection_value=0, has_mqtt=False, online=True)
        await device.dataRefresh(0)
        device.httpGet.assert_not_called()
