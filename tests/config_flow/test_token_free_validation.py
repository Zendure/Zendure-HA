from __future__ import annotations

import types

import pytest
from pytest_mock import MockerFixture


def _make_flow(mocker: MockerFixture, device_ip: str) -> object:
    from custom_components.zendure_ha.config_flow import ZendureConfigFlow
    from custom_components.zendure_ha.const import CONF_DEVICE_IP

    flow = mocker.MagicMock()
    flow._user_input = {}
    flow.async_set_unique_id = mocker.AsyncMock()
    flow._abort_if_unique_id_configured = mocker.MagicMock()
    flow.async_create_entry = mocker.MagicMock(return_value={"type": "create_entry"})
    flow.async_show_form = mocker.MagicMock(return_value={"type": "form"})
    flow.async_step_local = mocker.AsyncMock(return_value={"type": "form"})
    flow.async_step_user = types.MethodType(ZendureConfigFlow.async_step_user, flow)
    return flow


def _token_free_input(device_ip: str, *, mqttlocal: bool = False) -> dict:
    from custom_components.zendure_ha.const import (
        CONF_APPTOKEN,
        CONF_DEVICE_IP,
        CONF_MQTTLOCAL,
        CONF_MQTTLOG,
        CONF_P1METER,
    )

    return {
        CONF_APPTOKEN: "",
        CONF_DEVICE_IP: device_ip,
        CONF_MQTTLOCAL: mqttlocal,
        CONF_MQTTLOG: False,
        CONF_P1METER: "sensor.power",
    }


class TestTokenFreeValidation:
    """Token-free setup must validate the device_ip before creating a config entry."""

    @pytest.mark.asyncio
    async def test_unreachable_device_ip_shows_error(self, mocker: MockerFixture) -> None:
        """If LocalDiscovery returns None, async_step_user must stay in the form with an error."""
        mocker.patch(
            "custom_components.zendure_ha.config_flow.Api.Connect",
            new=mocker.AsyncMock(return_value=None),
        )

        flow = _make_flow(mocker, "192.168.10.99")
        result = await flow.async_step_user(_token_free_input("192.168.10.99"))

        flow.async_create_entry.assert_not_called()
        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_reachable_device_ip_creates_entry(self, mocker: MockerFixture) -> None:
        """If LocalDiscovery succeeds, async_step_user must create the config entry."""
        mocker.patch(
            "custom_components.zendure_ha.config_flow.Api.Connect",
            new=mocker.AsyncMock(return_value={"deviceList": [
                {"snNumber": "EOD1NLN9P010318", "ip": "192.168.10.80"},
            ]}),
        )

        flow = _make_flow(mocker, "192.168.10.80")
        await flow.async_step_user(_token_free_input("192.168.10.80"))

        flow.async_create_entry.assert_called_once()

    @pytest.mark.asyncio
    async def test_token_free_error_calls_connect_exactly_once(self, mocker: MockerFixture) -> None:
        """When token-free Connect fails, must NOT fall through to the second Connect call.

        Bug: when token_free+device_ip path sets errors["base"] (Connect returns None),
        there is no early return, so execution continues into the second
        `if await Api.Connect(...)` and calls the API a second time.
        """
        mock_connect = mocker.patch(
            "custom_components.zendure_ha.config_flow.Api.Connect",
            new=mocker.AsyncMock(return_value=None),
        )

        flow = _make_flow(mocker, "192.168.10.99")
        await flow.async_step_user(_token_free_input("192.168.10.99"))

        assert mock_connect.call_count == 1
