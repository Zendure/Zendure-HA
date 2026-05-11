from __future__ import annotations

import types

import pytest
from pytest_mock import MockerFixture


def _make_flow(mocker: MockerFixture, device_ip: str = "192.168.10.80") -> object:
    from custom_components.zendure_ha.config_flow import ZendureConfigFlow
    from custom_components.zendure_ha.const import CONF_DEVICE_IP

    flow = mocker.MagicMock()
    flow._user_input = {CONF_DEVICE_IP: device_ip}
    flow.async_set_unique_id = mocker.AsyncMock()
    flow._abort_if_unique_id_configured = mocker.MagicMock()
    flow.async_create_entry = mocker.MagicMock(return_value={"type": "create_entry"})
    flow.async_show_form = mocker.MagicMock(return_value={"type": "form"})
    flow.async_step_local = types.MethodType(ZendureConfigFlow.async_step_local, flow)
    return flow


def _local_input(*, auto_mqtt_setup: bool = False) -> dict:
    from custom_components.zendure_ha.const import (
        CONF_AUTO_MQTT_SETUP,
        CONF_MQTTPORT,
        CONF_MQTTPSW,
        CONF_MQTTSERVER,
        CONF_MQTTUSER,
    )

    return {
        CONF_MQTTSERVER: "192.168.10.10",
        CONF_MQTTPORT: 1883,
        CONF_MQTTUSER: "ha",
        CONF_MQTTPSW: "secret",
        CONF_AUTO_MQTT_SETUP: auto_mqtt_setup,
    }


class TestAsyncStepLocalErrors:
    """async_step_local must not create a config entry when any step fails."""

    @pytest.mark.asyncio
    async def test_connect_none_shows_error_no_entry(self, mocker: MockerFixture) -> None:
        """Api.Connect() -> None must show an error and not create an entry."""
        mocker.patch(
            "custom_components.zendure_ha.config_flow.Api.Connect",
            new=mocker.AsyncMock(return_value=None),
        )

        flow = _make_flow(mocker)
        await flow.async_step_local(_local_input())

        flow.async_create_entry.assert_not_called()
        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_mqtt_setup_false_shows_error_no_entry(self, mocker: MockerFixture) -> None:
        """ZenSdkMqttSetup() -> False must show mqtt_setup_failed and not create an entry."""
        device_ip = "192.168.10.80"
        sn = "EOD1NLN9P010318"

        mocker.patch(
            "custom_components.zendure_ha.config_flow.Api.Connect",
            new=mocker.AsyncMock(return_value={"deviceList": [
                {"snNumber": sn, "productModel": "solarFlow800Pro2", "ip": device_ip},
            ]}),
        )
        mocker.patch(
            "custom_components.zendure_ha.config_flow.Api.ZenSdkMqttSetup",
            new=mocker.AsyncMock(return_value=False),
        )

        flow = _make_flow(mocker, device_ip)
        await flow.async_step_local(_local_input(auto_mqtt_setup=True))

        flow.async_create_entry.assert_not_called()
        call_kwargs = flow.async_show_form.call_args
        errors = call_kwargs.kwargs.get("errors", {})
        assert errors.get("base") == "mqtt_setup_failed"

    @pytest.mark.asyncio
    async def test_auto_mqtt_setup_without_matching_sn_shows_error(self, mocker: MockerFixture) -> None:
        """auto_mqtt_setup with no matching zenSDK SN must show mqtt_setup_missing_device."""
        mocker.patch(
            "custom_components.zendure_ha.config_flow.Api.Connect",
            new=mocker.AsyncMock(return_value={"deviceList": [
                {"snNumber": "HYP001", "productModel": "hyper2000", "ip": ""},
            ]}),
        )

        # device_ip does not match any entry in deviceList → find_zensdk_sn returns ""
        flow = _make_flow(mocker, "192.168.10.80")
        await flow.async_step_local(_local_input(auto_mqtt_setup=True))

        flow.async_create_entry.assert_not_called()
        call_kwargs = flow.async_show_form.call_args
        errors = call_kwargs.kwargs.get("errors", {})
        assert errors.get("base") == "mqtt_setup_missing_device"

    @pytest.mark.asyncio
    async def test_auto_mqtt_setup_without_device_ip_shows_error(self, mocker: MockerFixture) -> None:
        """auto_mqtt_setup with empty device_ip must show mqtt_setup_missing_device."""
        from custom_components.zendure_ha.const import CONF_DEVICE_IP

        mocker.patch(
            "custom_components.zendure_ha.config_flow.Api.Connect",
            new=mocker.AsyncMock(return_value={"deviceList": [
                {"snNumber": "EOD1NLN9P010318", "productModel": "solarFlow800Pro2", "ip": "192.168.10.80"},
            ]}),
        )

        flow = _make_flow(mocker, "")
        flow._user_input = {CONF_DEVICE_IP: ""}
        await flow.async_step_local(_local_input(auto_mqtt_setup=True))

        flow.async_create_entry.assert_not_called()
        call_kwargs = flow.async_show_form.call_args
        errors = call_kwargs.kwargs.get("errors", {})
        assert errors.get("base") == "mqtt_setup_missing_device"

    @pytest.mark.asyncio
    async def test_successful_local_setup_creates_entry(self, mocker: MockerFixture) -> None:
        """Happy path: Connect succeeds, no auto_mqtt_setup → entry is created."""
        device_ip = "192.168.10.80"

        mocker.patch(
            "custom_components.zendure_ha.config_flow.Api.Connect",
            new=mocker.AsyncMock(return_value={"deviceList": [
                {"snNumber": "EOD1NLN9P010318", "productModel": "solarFlow800Pro2", "ip": device_ip},
            ]}),
        )

        flow = _make_flow(mocker, device_ip)
        await flow.async_step_local(_local_input())

        flow.async_create_entry.assert_called_once()
