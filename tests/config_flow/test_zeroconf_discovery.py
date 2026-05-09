"""Tests for mDNS/Zeroconf discovery flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.zendure_ha.config_flow import ZendureConfigFlow
from custom_components.zendure_ha.const import CONF_DEVICE_IP


def _make_discovery(host: str, name: str):
    """Build a minimal ZeroconfServiceInfo-like object."""
    info = MagicMock()
    info.host = host
    info.name = name
    return info


class TestZeroconfDiscovery:
    """mDNS discovery — async_step_zeroconf + async_step_zeroconf_confirm."""

    def _flow(self, hass=None):
        flow = ZendureConfigFlow()
        flow.hass = hass or MagicMock()
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()
        flow.async_show_form = MagicMock(return_value={"type": "form"})
        flow.async_step_user = AsyncMock(return_value={"type": "create_entry"})
        return flow

    @pytest.mark.asyncio
    async def test_sn_extracted_from_name(self):
        """SN is the last dash-segment of the mDNS name (without suffix)."""
        flow = self._flow()
        discovery = _make_discovery(
            host="192.168.10.80",
            name="Zendure-solarFlow800Pro2-EOD1NLN9P010318._http._tcp.local.",
        )
        await flow.async_step_zeroconf(discovery)
        flow.async_set_unique_id.assert_awaited_once_with("EOD1NLN9P010318")

    @pytest.mark.asyncio
    async def test_already_configured_updates_ip(self):
        """If SN is already known, only the IP is updated — no new entry."""
        flow = self._flow()
        flow._abort_if_unique_id_configured = MagicMock(side_effect=Exception("abort"))
        discovery = _make_discovery(
            host="192.168.10.81",
            name="Zendure-solarFlow800Pro2-EOD1NLN9P010318._http._tcp.local.",
        )
        with pytest.raises(Exception, match="abort"):
            await flow.async_step_zeroconf(discovery)
        flow._abort_if_unique_id_configured.assert_called_once_with(
            updates={CONF_DEVICE_IP: "192.168.10.81"}
        )

    @pytest.mark.asyncio
    async def test_confirm_shows_form_without_input(self):
        """Without user_input the confirm step shows a form."""
        flow = self._flow()
        flow._discovered = {"device_ip": "192.168.10.80", "sn": "EOD1NLN9P010318"}
        result = await flow.async_step_zeroconf_confirm(user_input=None)
        flow.async_show_form.assert_called_once_with(
            step_id="zeroconf_confirm",
            description_placeholders={"device_ip": "192.168.10.80", "sn": "EOD1NLN9P010318"},
        )

    @pytest.mark.asyncio
    async def test_confirm_with_input_proceeds_to_user_step(self):
        """With user_input the confirm step injects device_ip and calls async_step_user."""
        flow = self._flow()
        flow._discovered = {"device_ip": "192.168.10.80", "sn": "EOD1NLN9P010318"}
        result = await flow.async_step_zeroconf_confirm(user_input={})
        assert flow._user_input[CONF_DEVICE_IP] == "192.168.10.80"
        flow.async_step_user.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_full_discovery_flow(self):
        """End-to-end: discovery → confirm → user step."""
        flow = self._flow()
        discovery = _make_discovery(
            host="192.168.10.80",
            name="Zendure-solarFlow800Pro2-EOD1NLN9P010318._http._tcp.local.",
        )
        # First call: discovery sets _discovered and shows confirm form
        await flow.async_step_zeroconf(discovery)
        assert flow._discovered == {"device_ip": "192.168.10.80", "sn": "EOD1NLN9P010318"}
        # Second call: user confirms
        result = await flow.async_step_zeroconf_confirm(user_input={})
        assert result == {"type": "create_entry"}
