"""Regression tests for Connect(reload=True) storage guard.

When cloud returns empty deviceList but local discovery succeeds, the result
is a local-only fallback. Connect() must NOT overwrite cached cloud devices
in storage with this degraded partial view.
"""
from __future__ import annotations

import pytest
from pytest_mock import MockerFixture

from tests.api.helpers import CLOUD_EMPTY, CLOUD_WITH_HYPER, hass  # noqa: F401


class TestConnectReloadStorageGuard:

    TOKEN = "aHR0cHM6Ly9hcHAuemVuZHVyZS50ZWNoL2V1LjQ4clFLRGFUOQ=="

    def _data(self, device_ip: str = "192.168.10.80") -> dict:
        from custom_components.zendure_ha.const import CONF_DEVICE_IP
        return {"token": self.TOKEN, CONF_DEVICE_IP: device_ip}

    @pytest.mark.asyncio
    async def test_local_fallback_does_not_overwrite_storage(
        self, hass: object, mocker: MockerFixture
    ) -> None:
        """Cloud empty + LocalDiscovery succeeds → storage.async_save must NOT be called."""
        from custom_components.zendure_ha.api import Api

        local_result = {
            "mqtt": {},
            "deviceList": [{"deviceKey": "EOD1", "productModel": "solarFlow800Pro2",
                            "deviceName": "solarFlow800Pro2", "snNumber": "EOD1", "ip": "192.168.10.80"}],
        }

        mock_store = mocker.MagicMock()
        mock_store.async_load = mocker.AsyncMock(return_value=None)
        mock_store.async_save = mocker.AsyncMock()
        mocker.patch("custom_components.zendure_ha.api.Store", return_value=mock_store)
        mocker.patch.object(Api, "ApiHA", new=mocker.AsyncMock(return_value={
            **local_result, "_local_fallback_only": True,
        }))

        result = await Api.Connect(hass, self._data(), reload=True)

        mock_store.async_save.assert_not_called()
        assert result is not None
        assert "_local_fallback_only" not in result

    @pytest.mark.asyncio
    async def test_healthy_cloud_result_saves_to_storage(
        self, hass: object, mocker: MockerFixture
    ) -> None:
        """Healthy cloud result (non-empty, no fallback flag) → storage.async_save called."""
        from custom_components.zendure_ha.api import Api

        mock_store = mocker.MagicMock()
        mock_store.async_load = mocker.AsyncMock(return_value=None)
        mock_store.async_save = mocker.AsyncMock()
        mocker.patch("custom_components.zendure_ha.api.Store", return_value=mock_store)
        mocker.patch.object(Api, "ApiHA", new=mocker.AsyncMock(return_value=CLOUD_WITH_HYPER["data"]))

        await Api.Connect(hass, self._data(), reload=True)

        mock_store.async_save.assert_called_once()
