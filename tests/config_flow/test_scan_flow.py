"""Tests for the manual mDNS scan flow (menu → scan → scan_confirm → user_form)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.zendure_ha.config_flow import ZendureConfigFlow
from custom_components.zendure_ha.const import CONF_DEVICE_IP

_SCAN_PATH = "custom_components.zendure_ha.config_flow._scan_for_zendure"
_LOCAL_DISCOVERY_PATH = "custom_components.zendure_ha.config_flow.Api.LocalDiscovery"

_SCAN_RESULT = {"device_ip": "192.168.10.80", "sn": "EOD1NLN9P010318"}
_DISCOVERY_RESPONSE = {
    "deviceList": [{"snNumber": "EOD1NLN9P010318", "productModel": "solarFlow800Pro2"}]
}


def _make_flow():
    flow = ZendureConfigFlow()
    flow.hass = MagicMock()
    setattr(flow, "async_set_unique_id", AsyncMock())
    setattr(flow, "_abort_if_unique_id_configured", MagicMock())
    setattr(flow, "async_show_menu", MagicMock(return_value={"type": "menu"}))
    setattr(flow, "async_show_form", MagicMock(return_value={"type": "form"}))
    setattr(flow, "async_show_progress", MagicMock(return_value={"type": "progress"}))
    setattr(
        flow,
        "async_show_progress_done",
        MagicMock(return_value={"type": "progress_done"}),
    )
    flow.async_step_user_form = AsyncMock(return_value={"type": "form"})
    return flow


class TestAsyncStepScan:
    """async_step_scan: progress spinner while scanning."""

    @pytest.mark.asyncio
    async def test_shows_progress_while_task_running(self):
        """Returns a progress card while the background scan task is running."""
        flow = _make_flow()
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        flow.hass.async_create_task = MagicMock(return_value=future)

        result = await flow.async_step_scan()

        assert result["type"] == "progress"
        flow.async_show_progress.assert_called_once()
        assert flow.async_show_progress.call_args.kwargs["step_id"] == "scan"
        assert flow.async_show_progress.call_args.kwargs["progress_action"] == "scanning"

    @pytest.mark.asyncio
    async def test_scan_miss_falls_back_to_user_form(self):
        """When no device is found the flow falls back to the manual entry form."""
        flow = _make_flow()
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        future.set_result(None)
        flow.hass.async_create_task = MagicMock(return_value=future)

        await flow.async_step_scan()

        flow.async_step_user_form.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_scan_exception_falls_back_to_user_form(self):
        """An exception in the scan task is swallowed and falls back to manual entry."""
        flow = _make_flow()
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        future.set_exception(OSError("network unreachable"))
        flow.hass.async_create_task = MagicMock(return_value=future)

        await flow.async_step_scan()

        flow.async_step_user_form.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_scan_success_goes_to_scan_confirm(self):
        """When a device is found the flow proceeds to the confirm dialog."""
        flow = _make_flow()
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        future.set_result(_SCAN_RESULT)
        flow.hass.async_create_task = MagicMock(return_value=future)
        flow.async_step_scan_confirm = AsyncMock(return_value={"type": "form"})

        await flow.async_step_scan()

        flow.async_step_scan_confirm.assert_awaited_once()
        assert flow._discovered == _SCAN_RESULT


class TestScanAndEnrich:
    """_scan_and_enrich: LocalDiscovery enrichment during background scan."""

    @pytest.mark.asyncio
    async def test_enriches_sn_and_model_from_local_discovery(self):
        """LocalDiscovery SN and model override the raw mDNS values."""
        flow = _make_flow()
        with (
            patch(_SCAN_PATH, AsyncMock(return_value=dict(_SCAN_RESULT))),
            patch(_LOCAL_DISCOVERY_PATH, AsyncMock(return_value=_DISCOVERY_RESPONSE)),
        ):
            result = await flow._scan_and_enrich()

        assert result is not None
        assert result["sn"] == "EOD1NLN9P010318"
        assert result["model"] == "solarFlow800Pro2"

    @pytest.mark.asyncio
    async def test_local_discovery_failure_keeps_mdns_sn(self):
        """If LocalDiscovery returns None the mDNS-extracted SN is kept."""
        flow = _make_flow()
        with (
            patch(_SCAN_PATH, AsyncMock(return_value=dict(_SCAN_RESULT))),
            patch(_LOCAL_DISCOVERY_PATH, AsyncMock(return_value=None)),
        ):
            result = await flow._scan_and_enrich()

        assert result is not None
        assert result["sn"] == "EOD1NLN9P010318"
        assert result["model"] == ""

    @pytest.mark.asyncio
    async def test_no_device_on_network_returns_none(self):
        """When the mDNS scan finds nothing _scan_and_enrich returns None."""
        flow = _make_flow()
        with patch(_SCAN_PATH, AsyncMock(return_value=None)):
            result = await flow._scan_and_enrich()

        assert result is None


class TestAsyncStepScanConfirm:
    """async_step_scan_confirm: editable IP field, proceeds to user_form."""

    @pytest.mark.asyncio
    async def test_shows_form_with_prefilled_ip(self):
        """Confirm dialog shows IP and SN from the discovered device."""
        flow = _make_flow()
        flow._discovered = {"device_ip": "192.168.10.80", "sn": "EOD1NLN9P010318", "model": "solarFlow800Pro2"}

        await flow.async_step_scan_confirm(user_input=None)

        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["step_id"] == "scan_confirm"
        assert call_kwargs["description_placeholders"] == flow._discovered

    @pytest.mark.asyncio
    async def test_user_can_override_ip(self):
        """The user can change the IP before confirming."""
        flow = _make_flow()
        flow._discovered = {"device_ip": "192.168.10.80", "sn": "EOD1NLN9P010318", "model": ""}
        flow._user_input = {}

        await flow.async_step_scan_confirm(user_input={CONF_DEVICE_IP: "192.168.10.99"})

        assert flow._user_input[CONF_DEVICE_IP] == "192.168.10.99"
        assert flow._discovered["device_ip"] == "192.168.10.99"

    @pytest.mark.asyncio
    async def test_confirm_proceeds_to_user_form(self):
        """Submitting the confirm dialog forwards to user_form."""
        flow = _make_flow()
        flow._discovered = {"device_ip": "192.168.10.80", "sn": "EOD1NLN9P010318", "model": ""}
        flow._user_input = {}

        await flow.async_step_scan_confirm(user_input={CONF_DEVICE_IP: "192.168.10.80"})

        flow.async_step_user_form.assert_awaited_once()
