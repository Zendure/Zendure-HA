"""Tests for mDNS/Zeroconf discovery flow."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.zendure_ha.config_flow import ZendureConfigFlow
from custom_components.zendure_ha.const import CONF_DEVICE_IP

_LOCAL_DISCOVERY_PATH = "custom_components.zendure_ha.config_flow.Api.LocalDiscovery"

_DISCOVERY_RESPONSE = {
    "deviceList": [{"snNumber": "EOD1NLN9P010318", "productModel": "solarFlow800Pro2"}]
}


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
    """SN extraction, unique_id handling, and LocalDiscovery enrichment."""

    @pytest.mark.asyncio
    async def test_unique_id_always_zendure(self):
        """unique_id is always 'Zendure' — consistent with the manual config flow."""
        flow = _make_flow()
        with patch(_LOCAL_DISCOVERY_PATH, AsyncMock(return_value=_DISCOVERY_RESPONSE)):
            await flow.async_step_zeroconf(
                _make_discovery(
                    "192.168.10.80",
                    "Zendure-solarFlow800Pro2-EOD1NLN9P010318._http._tcp.local.",
                )
            )
        flow.async_set_unique_id.assert_awaited_once_with("Zendure")

    @pytest.mark.asyncio
    async def test_sn_confirmed_by_local_discovery(self):
        """SN from LocalDiscovery overrides the mDNS-name-extracted SN."""
        flow = _make_flow()
        with patch(_LOCAL_DISCOVERY_PATH, AsyncMock(return_value=_DISCOVERY_RESPONSE)):
            await flow.async_step_zeroconf(
                _make_discovery(
                    "192.168.10.80",
                    "Zendure-solarFlow800Pro2-EOD1NLN9P010318._http._tcp.local.",
                )
            )
        assert flow._discovered["sn"] == "EOD1NLN9P010318"

    @pytest.mark.asyncio
    async def test_model_stored_from_local_discovery(self):
        """Model name from LocalDiscovery is stored for the confirm dialog."""
        flow = _make_flow()
        with patch(_LOCAL_DISCOVERY_PATH, AsyncMock(return_value=_DISCOVERY_RESPONSE)):
            await flow.async_step_zeroconf(
                _make_discovery(
                    "192.168.10.80",
                    "Zendure-solarFlow800Pro2-EOD1NLN9P010318._http._tcp.local.",
                )
            )
        assert flow._discovered["model"] == "solarFlow800Pro2"

    @pytest.mark.asyncio
    async def test_local_discovery_failure_still_shows_confirm(self):
        """If LocalDiscovery fails the flow continues with mDNS-extracted SN."""
        flow = _make_flow()
        with patch(_LOCAL_DISCOVERY_PATH, AsyncMock(return_value=None)):
            await flow.async_step_zeroconf(
                _make_discovery(
                    "192.168.10.80",
                    "Zendure-solarFlow800Pro2-EOD1NLN9P010318._http._tcp.local.",
                )
            )
        assert flow._discovered["sn"] == "EOD1NLN9P010318"
        assert flow._discovered["model"] == ""

    @pytest.mark.asyncio
    async def test_sn_from_zendure_tcp_service_type(self):
        """SN extraction works for _zendure._tcp.local. service type too."""
        flow = _make_flow()
        with patch(_LOCAL_DISCOVERY_PATH, AsyncMock(return_value=_DISCOVERY_RESPONSE)):
            await flow.async_step_zeroconf(
                _make_discovery(
                    "192.168.10.80",
                    "Zendure-solarFlow800Pro2-EOD1NLN9P010318._zendure._tcp.local.",
                )
            )
        assert flow._discovered["sn"] == "EOD1NLN9P010318"

    @pytest.mark.asyncio
    async def test_already_configured_updates_ip_only(self):
        """If 'Zendure' entry exists, only device_ip is updated — no new entry."""
        flow = _make_flow()
        flow._abort_if_unique_id_configured = MagicMock(side_effect=Exception("abort"))
        with (
            patch(_LOCAL_DISCOVERY_PATH, AsyncMock(return_value=_DISCOVERY_RESPONSE)),
            pytest.raises(Exception, match="abort"),
        ):
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
