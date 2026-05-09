"""Tests for mDNS/Zeroconf discovery flow."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.zendure_ha.config_flow import ZendureConfigFlow
from custom_components.zendure_ha.const import CONF_DEVICE_IP


def _make_discovery(host: str, name: str):
    """Build a minimal ZeroconfServiceInfo-like object."""
    info = MagicMock()
    info.host = host
    info.name = name
    return info


def _make_flow():
    flow = ZendureConfigFlow()
    flow.hass = MagicMock()
    setattr(flow, "async_set_unique_id", AsyncMock())
    setattr(flow, "_abort_if_unique_id_configured", MagicMock())
    setattr(flow, "async_show_form", MagicMock(return_value={"type": "form"}))
    setattr(flow, "async_show_progress", MagicMock(return_value={"type": "progress"}))
    setattr(
        flow,
        "async_show_progress_done",
        MagicMock(return_value={"type": "progress_done"}),
    )
    setattr(flow, "async_step_user", AsyncMock(return_value={"type": "create_entry"}))
    return flow


class TestZeroconfSNExtraction:
    """SN extraction works for both mDNS service types."""

    @pytest.mark.asyncio
    async def test_sn_from_http_tcp(self):
        flow = _make_flow()
        await flow.async_step_zeroconf(
            _make_discovery(
                "192.168.10.80",
                "Zendure-solarFlow800Pro2-EOD1NLN9P010318._http._tcp.local.",
            )
        )
        flow.async_set_unique_id.assert_awaited_once_with("EOD1NLN9P010318")

    @pytest.mark.asyncio
    async def test_sn_from_zendure_tcp(self):
        flow = _make_flow()
        await flow.async_step_zeroconf(
            _make_discovery(
                "192.168.10.80",
                "Zendure-solarFlow800Pro2-EOD1NLN9P010318._zendure._tcp.local.",
            )
        )
        flow.async_set_unique_id.assert_awaited_once_with("EOD1NLN9P010318")

    @pytest.mark.asyncio
    async def test_already_configured_updates_ip_only(self):
        flow = _make_flow()
        flow._abort_if_unique_id_configured = MagicMock(side_effect=Exception("abort"))
        with pytest.raises(Exception, match="abort"):
            await flow.async_step_zeroconf(
                _make_discovery(
                    "192.168.10.81",
                    "Zendure-solarFlow800Pro2-EOD1NLN9P010318._http._tcp.local.",
                )
            )
        flow._abort_if_unique_id_configured.assert_called_once_with(
            updates={CONF_DEVICE_IP: "192.168.10.81"}
        )


class TestZeroconfConfirm:
    """zeroconf_confirm: editable IP field, description placeholders."""

    @pytest.mark.asyncio
    async def test_shows_form_with_ip_field(self):
        flow = _make_flow()
        flow._discovered = {"device_ip": "192.168.10.80", "sn": "EOD1NLN9P010318"}
        await flow.async_step_zeroconf_confirm(user_input=None)
        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["step_id"] == "zeroconf_confirm"
        assert call_kwargs["description_placeholders"] == {
            "device_ip": "192.168.10.80",
            "sn": "EOD1NLN9P010318",
        }

    @pytest.mark.asyncio
    async def test_user_can_override_ip(self):
        flow = _make_flow()
        flow._discovered = {"device_ip": "192.168.10.80", "sn": "EOD1NLN9P010318"}
        flow.async_step_zeroconf_connect = AsyncMock(return_value={"type": "progress"})
        await flow.async_step_zeroconf_confirm(
            user_input={CONF_DEVICE_IP: "192.168.10.99"}
        )
        assert flow._user_input[CONF_DEVICE_IP] == "192.168.10.99"
        assert flow._discovered["device_ip"] == "192.168.10.99"

    @pytest.mark.asyncio
    async def test_confirm_proceeds_to_connect(self):
        flow = _make_flow()
        flow._discovered = {"device_ip": "192.168.10.80", "sn": "EOD1NLN9P010318"}
        flow.async_step_zeroconf_connect = AsyncMock(return_value={"type": "progress"})
        await flow.async_step_zeroconf_confirm(
            user_input={CONF_DEVICE_IP: "192.168.10.80"}
        )
        flow.async_step_zeroconf_connect.assert_awaited_once()


class TestZeroconfConnect:
    """Progress spinner while connecting."""

    @pytest.mark.asyncio
    async def test_shows_progress_while_task_running(self):
        flow = _make_flow()
        flow._discovered = {"device_ip": "192.168.10.80", "sn": "EOD1NLN9P010318"}
        flow._user_input = {CONF_DEVICE_IP: "192.168.10.80"}

        # Task that never completes during the test
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        flow.hass.async_create_task = MagicMock(return_value=future)

        await flow.async_step_zeroconf_connect()
        flow.async_show_progress.assert_called_once()
        assert (
            flow.async_show_progress.call_args.kwargs["step_id"] == "zeroconf_connect"
        )
        assert (
            flow.async_show_progress.call_args.kwargs["progress_action"] == "connecting"
        )

    @pytest.mark.asyncio
    async def test_success_goes_to_user_step(self):
        flow = _make_flow()
        flow._user_input = {CONF_DEVICE_IP: "192.168.10.80"}

        done_future: asyncio.Future = asyncio.get_event_loop().create_future()
        done_future.set_result({"deviceList": []})
        flow.hass.async_create_task = MagicMock(return_value=done_future)

        await flow.async_step_zeroconf_connect()
        flow.async_show_progress_done.assert_called_once_with(next_step_id="user")

    @pytest.mark.asyncio
    async def test_connect_failure_goes_to_zeroconf_failed(self):
        flow = _make_flow()
        flow._user_input = {CONF_DEVICE_IP: "192.168.10.80"}

        done_future: asyncio.Future = asyncio.get_event_loop().create_future()
        done_future.set_result(None)  # Api.Connect returns None on failure
        flow.hass.async_create_task = MagicMock(return_value=done_future)

        await flow.async_step_zeroconf_connect()
        flow.async_show_progress_done.assert_called_once_with(
            next_step_id="zeroconf_failed"
        )

    @pytest.mark.asyncio
    async def test_exception_in_task_goes_to_zeroconf_failed(self):
        flow = _make_flow()
        flow._user_input = {CONF_DEVICE_IP: "192.168.10.80"}

        done_future: asyncio.Future = asyncio.get_event_loop().create_future()
        done_future.set_exception(OSError("connection refused"))
        flow.hass.async_create_task = MagicMock(return_value=done_future)

        await flow.async_step_zeroconf_connect()
        flow.async_show_progress_done.assert_called_once_with(
            next_step_id="zeroconf_failed"
        )


class TestZeroconfFailed:
    """Connection failure bounces back to confirm form with error."""

    @pytest.mark.asyncio
    async def test_shows_confirm_form_with_error(self):
        flow = _make_flow()
        flow._discovered = {"device_ip": "192.168.10.80", "sn": "EOD1NLN9P010318"}
        await flow.async_step_zeroconf_failed()
        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["step_id"] == "zeroconf_confirm"
        assert call_kwargs["errors"] == {"base": "cannot_connect"}
