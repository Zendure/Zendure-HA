"""P1 meter: local report is mapped onto power sensors."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from custom_components.zendure_ha.devices.p1meter import P1Meter

REPORT = '{"timestamp":1,"deviceId":"6R16mUea","a_aprt_power":-7,"b_aprt_power":0,"c_aprt_power":0,"total_power":-7,"meterType":3,"protocolType":41}'


def test_model_detection() -> None:
    assert P1Meter.isP1Meter("Zendure P1 Meter")
    assert P1Meter.isP1Meter("P1")
    assert not P1Meter.isP1Meter("SolarFlow 800")


async def test_poll_updates_power_entities(hass) -> None:  # noqa: ANN001
    meter = P1Meter(hass, "6R16mUea", "P1", {"productModel": "P1", "productKey": "k", "snNumber": "SN1", "ip": "192.168.0.213"})
    response = MagicMock()
    response.text = AsyncMock(return_value=REPORT)
    meter.session = MagicMock(get=AsyncMock(return_value=response))
    meter.entityUpdate = MagicMock(wraps=meter.entityUpdate)

    await meter._poll(None)  # type: ignore[arg-type]

    assert set(meter.entities) == {"total_power", "a_aprt_power", "b_aprt_power", "c_aprt_power"}
    meter.entityUpdate.assert_any_call("total_power", -7)
    meter.session.get.assert_awaited_once()
    assert meter.session.get.await_args.args[0] == "http://192.168.0.213/properties/report"
