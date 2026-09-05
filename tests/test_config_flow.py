"""Tests for the Zendure config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock

from pytest_homeassistant.common import MockConfigEntry

from custom_components.zendure_ha.api import Api
from custom_components.zendure_ha.config_flow import ZendureOptionsFlowHandler
from custom_components.zendure_ha.const import (
    CONF_APPTOKEN,
    CONF_LOCAL_IP_SECTION,
    CONF_LOCAL_IPS,
    CONF_MQTTLOG,
    CONF_OVERWRITE_IP,
    CONF_P1METER,
    CONF_USE_MDNS,
    DOMAIN,
)


async def test_options_flow_builds_and_saves_device_ip_fields(hass, monkeypatch) -> None:  # noqa: ANN001
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_APPTOKEN: "token",
            CONF_P1METER: "sensor.power",
            CONF_MQTTLOG: False,
        },
    )
    entry.add_to_hass(hass)
    monkeypatch.setattr(
        Api,
        "Connect",
        AsyncMock(
            return_value={
                "deviceList": [
                    {"snNumber": "SN-ONE", "ip": "192.0.2.10"},
                    {"snNumber": "SN-TWO", "ip": "192.0.2.11"},
                ]
            }
        ),
    )

    flow = ZendureOptionsFlowHandler()
    flow.hass = hass
    flow.handler = entry.entry_id

    result = await flow.async_step_init()

    assert result["type"] == "form"
    section_schema = result["data_schema"].schema[CONF_LOCAL_IP_SECTION]
    fields = section_schema.schema.schema
    assert {field.schema for field in fields} == {"SN-ONE", "SN-TWO"}
    suggested_ips = {
        field.schema: field.description["suggested_value"] for field in fields
    }
    assert suggested_ips == {
        "SN-ONE": "192.0.2.10",
        "SN-TWO": "192.0.2.11",
    }

    result = await flow.async_step_init(
        {
            CONF_P1METER: "sensor.power",
            CONF_MQTTLOG: False,
            CONF_USE_MDNS: True,
            CONF_LOCAL_IP_SECTION: {
                "SN-ONE": "192.168.1.50",
                "SN-TWO": "192.168.1.51",
            },
            CONF_OVERWRITE_IP: True,
        }
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_LOCAL_IPS] == {
        "SN-ONE": "192.168.1.50",
        "SN-TWO": "192.168.1.51",
    }


async def test_options_flow_prefers_saved_ips_over_cloud_ips(hass, monkeypatch) -> None:  # noqa: ANN001
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_APPTOKEN: "token",
            CONF_P1METER: "sensor.power",
            CONF_MQTTLOG: False,
            CONF_LOCAL_IPS: {"SN-ONE": "192.168.1.50"},
            CONF_OVERWRITE_IP: True,
        },
    )
    entry.add_to_hass(hass)
    monkeypatch.setattr(
        Api,
        "Connect",
        AsyncMock(
            return_value={
                "deviceList": [
                    {"snNumber": "SN-ONE", "ip": "192.0.2.10"},
                ]
            }
        ),
    )

    flow = ZendureOptionsFlowHandler()
    flow.hass = hass
    flow.handler = entry.entry_id
    result = await flow.async_step_init()

    section_schema = result["data_schema"].schema[CONF_LOCAL_IP_SECTION]
    field = next(iter(section_schema.schema.schema))
    assert field.description["suggested_value"] == "192.168.1.50"


async def test_options_flow_uses_cloud_ips_when_overwrite_is_disabled(hass, monkeypatch) -> None:  # noqa: ANN001
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_APPTOKEN: "token",
            CONF_P1METER: "sensor.power",
            CONF_MQTTLOG: False,
            CONF_LOCAL_IPS: {"SN-ONE": "192.168.1.50"},
            CONF_OVERWRITE_IP: False,
        },
    )
    entry.add_to_hass(hass)
    monkeypatch.setattr(
        Api,
        "Connect",
        AsyncMock(
            return_value={
                "deviceList": [{"snNumber": "SN-ONE", "ip": "192.0.2.10"}]
            }
        ),
    )

    flow = ZendureOptionsFlowHandler()
    flow.hass = hass
    flow.handler = entry.entry_id
    result = await flow.async_step_init()

    section_schema = result["data_schema"].schema[CONF_LOCAL_IP_SECTION]
    field = next(iter(section_schema.schema.schema))
    assert field.description["suggested_value"] == "192.0.2.10"

    result = await flow.async_step_init(
        {
            CONF_P1METER: "sensor.power",
            CONF_MQTTLOG: False,
            CONF_USE_MDNS: True,
            CONF_OVERWRITE_IP: False,
            CONF_LOCAL_IP_SECTION: {"SN-ONE": "192.168.1.60"},
        }
    )

    assert result["data"][CONF_LOCAL_IPS] == {}
