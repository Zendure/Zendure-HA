from __future__ import annotations

import pytest
from pytest_mock import MockerFixture

from tests.api.helpers import hass  # noqa: F401


class TestConnectStorageFallback:
    """Connect(reload=True) must fall back to storage when deviceList is empty."""

    @pytest.mark.asyncio
    async def test_empty_device_list_triggers_storage_fallback(
        self, hass: object, mocker: MockerFixture
    ) -> None:
        """ApiHA returns {mqtt: {...}, deviceList: []} → Connect must use storage, not that result."""
        from custom_components.zendure_ha.api import Api

        stored_devices = {"deviceList": [{"snNumber": "HYP001", "productModel": "hyper2000"}]}

        mocker.patch.object(
            Api, "ApiHA",
            new=mocker.AsyncMock(return_value={"mqtt": {"clientId": "c1"}, "deviceList": []}),
        )
        mock_store = mocker.MagicMock()
        mock_store.async_load = mocker.AsyncMock(return_value={"devices": stored_devices})
        mock_store.async_save = mocker.AsyncMock()
        mocker.patch("custom_components.zendure_ha.api.Store", return_value=mock_store)

        result = await Api.Connect(hass, {"token": "tok"}, reload=True)

        assert result == stored_devices, (
            f"Expected storage fallback devices, got '{result}'. "
            "A dict with empty deviceList must not be treated as a valid non-empty result."
        )

    @pytest.mark.asyncio
    async def test_none_result_triggers_storage_fallback(
        self, hass: object, mocker: MockerFixture
    ) -> None:
        """ApiHA returns None → Connect still falls back to storage (existing behaviour)."""
        from custom_components.zendure_ha.api import Api

        stored_devices = {"deviceList": [{"snNumber": "HYP001"}]}

        mocker.patch.object(Api, "ApiHA", new=mocker.AsyncMock(return_value=None))
        mock_store = mocker.MagicMock()
        mock_store.async_load = mocker.AsyncMock(return_value={"devices": stored_devices})
        mock_store.async_save = mocker.AsyncMock()
        mocker.patch("custom_components.zendure_ha.api.Store", return_value=mock_store)

        result = await Api.Connect(hass, {"token": "tok"}, reload=True)

        assert result == stored_devices
