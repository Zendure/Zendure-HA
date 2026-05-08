from __future__ import annotations

import pytest
from pytest_mock import MockerFixture

from tests.api.helpers import PROPERTIES_REPORT, _mock_http_response, hass  # noqa: F401


class TestLocalDiscovery:

    @pytest.mark.asyncio
    async def test_happy_path(self, hass: object, mocker: MockerFixture) -> None:
        """Device at IP responds → returns synthesized deviceList entry."""
        import custom_components.zendure_ha.api as api_mod
        from custom_components.zendure_ha.api import Api

        session = mocker.MagicMock()
        session.get = _mock_http_response(mocker, PROPERTIES_REPORT)
        mocker.patch.object(api_mod, "async_get_clientsession", return_value=session)

        result = await Api.LocalDiscovery(hass, "192.168.10.80")

        assert result is not None
        assert result["mqtt"] == {}
        dev = result["deviceList"][0]
        assert dev["deviceKey"] == "EOD1NLN9P010318"
        assert dev["productModel"] == "solarFlow800Pro2"
        assert dev["snNumber"] == "EOD1NLN9P010318"
        assert dev["ip"] == "192.168.10.80"

    @pytest.mark.asyncio
    async def test_missing_fields_returns_none(self, hass: object, mocker: MockerFixture) -> None:
        """Response without sn/product → None."""
        import custom_components.zendure_ha.api as api_mod
        from custom_components.zendure_ha.api import Api

        session = mocker.MagicMock()
        session.get = _mock_http_response(mocker, {"timestamp": 1})
        mocker.patch.object(api_mod, "async_get_clientsession", return_value=session)

        assert await Api.LocalDiscovery(hass, "192.168.10.80") is None

    @pytest.mark.asyncio
    async def test_network_error_returns_none(self, hass: object, mocker: MockerFixture) -> None:
        """Device unreachable → None, no exception propagated."""
        import custom_components.zendure_ha.api as api_mod
        from custom_components.zendure_ha.api import Api

        session = mocker.MagicMock()
        session.get = mocker.MagicMock(side_effect=OSError("connection refused"))
        mocker.patch.object(api_mod, "async_get_clientsession", return_value=session)

        assert await Api.LocalDiscovery(hass, "192.168.10.80") is None
