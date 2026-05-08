from __future__ import annotations

import types

import pytest
from pytest_mock import MockerFixture


def _make_flow(mocker: MockerFixture, device_ip: str) -> object:
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


class TestAsyncStepLocalSnSelection:
    """async_step_local must call ZenSdkMqttSetup with the zenSDK device SN, not device_list[0]."""

    @pytest.mark.asyncio
    async def test_mixed_setup_passes_zensdk_sn_to_mqtt_setup(self, mocker: MockerFixture) -> None:
        """In a mixed device list, ZenSdkMqttSetup receives the SN matching device_ip."""
        from custom_components.zendure_ha.const import (
            CONF_AUTO_MQTT_SETUP,
            CONF_MQTTPORT,
            CONF_MQTTPSW,
            CONF_MQTTSERVER,
            CONF_MQTTUSER,
        )

        device_ip = "192.168.10.80"
        zensdk_sn = "EOD1NLN9P010318"
        mixed_list = [
            {"snNumber": "HYP001", "productModel": "hyper2000", "ip": ""},
            {"snNumber": zensdk_sn, "productModel": "solarFlow800Pro2", "ip": device_ip},
        ]

        mocker.patch(
            "custom_components.zendure_ha.config_flow.Api.Connect",
            new=mocker.AsyncMock(return_value={"deviceList": mixed_list}),
        )
        mock_mqtt_setup = mocker.patch(
            "custom_components.zendure_ha.config_flow.Api.ZenSdkMqttSetup",
            new=mocker.AsyncMock(),
        )

        flow = _make_flow(mocker, device_ip)
        user_input = {
            CONF_MQTTSERVER: "192.168.10.10",
            CONF_MQTTPORT: 1883,
            CONF_MQTTUSER: "ha",
            CONF_MQTTPSW: "secret",
            CONF_AUTO_MQTT_SETUP: True,
        }

        await flow.async_step_local(user_input)

        mock_mqtt_setup.assert_called_once()
        called_sn = mock_mqtt_setup.call_args.args[2]  # (hass, device_ip, sn, ...)
        assert called_sn == zensdk_sn, (
            f"ZenSdkMqttSetup called with SN '{called_sn}', "
            f"expected zenSDK SN '{zensdk_sn}' (device_list[0] is the legacy Hyper 2000)"
        )
