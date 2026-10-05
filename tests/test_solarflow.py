"""ZendureSolarFlow: power and entity_write routing per ConnectionType, hemsEP for the SolarFlows, deviceAutomation for the legacy models."""

import asyncio
import json
import time

import custom_components.zendure_ha.zendure.solarflow as solarflow_module
from custom_components.zendure_ha.device import DeviceState
from custom_components.zendure_ha.zendure.device import Command, ConnectionType
from custom_components.zendure_ha.zendure.solarflow import ZendureHub, ZendureHyper, ZendureLegacy, ZendureSolarFlow, _limits


class FakeMqtt:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, json.loads(payload)))


class FakeResponse:
    def __init__(self, body: str, status: int = 200) -> None:
        self._body = body
        self.status = status

    async def text(self) -> str:
        return self._body


class FakeSession:
    def __init__(self, report: dict) -> None:
        self.report = report
        self.posts: list[tuple[str, str]] = []

    async def post(self, url: str, data=None, headers=None, timeout=None) -> FakeResponse:  # noqa: ANN001
        self.posts.append((url, data))
        return FakeResponse("{}")

    async def get(self, url: str, headers=None, timeout=None) -> FakeResponse:  # noqa: ANN001
        await asyncio.sleep(0.05)  # keep SENT observable between the post and the report
        return FakeResponse(json.dumps(self.report))


def make_device(hass, model: str = "Solarflow 2400 AC", cls: type = ZendureSolarFlow, pwr_min: int = -2400) -> ZendureSolarFlow:  # noqa: ANN001
    d = cls(hass, None, None, "SF2400_D9", {"product": model, "sn": "AB3CD9", "device_id": "simdev", "prodkey": "gDa3tb"}, model, 2400, pwr_min, 800)
    d.mqtt = FakeMqtt()
    return d


async def wait_for(predicate, timeout: float = 2.0) -> bool:  # noqa: ANN001
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_power_local_publishes_the_limits(hass) -> None:  # noqa: ANN001
    d = make_device(hass)
    d.connection = ConnectionType.LOCAL

    # a SolarFlow takes the limits as one property write, no hemsEP
    d.power(700)
    topic, cmd = d.mqtt.published[0]
    assert topic == "iot/gDa3tb/simdev/properties/write"
    assert cmd["properties"] == {"smartMode": 1, "acMode": 2, "outputLimit": 700, "inputLimit": 0}

    d.power_since = 0.0  # the hold after the last command has passed
    d.power(-500)
    _, cmd = d.mqtt.published[1]
    assert cmd["properties"] == {"smartMode": 1, "acMode": 1, "outputLimit": 0, "inputLimit": 500}

    d.power_since = 0.0
    d.power(0)
    _, cmd = d.mqtt.published[2]
    assert cmd["properties"] == {"smartMode": 0, "acMode": 2, "outputLimit": 0, "inputLimit": 0}
    d.unload()


async def test_power_off_publishes_off_command(hass) -> None:  # noqa: ANN001
    d = make_device(hass)
    d.connection = ConnectionType.LOCAL
    d.power_off()
    topic, cmd = d.mqtt.published[0]
    assert topic == "iot/gDa3tb/simdev/properties/write" and cmd["properties"] == {"smartMode": 0, "acMode": 2, "outputLimit": 0, "inputLimit": 0}  # 0 W first
    topic, cmd = d.mqtt.published[1]
    assert topic == "iot/gDa3tb/simdev/properties/write" and cmd["properties"] == {"hemsState": 0}  # then the hems flag down
    assert d.state == DeviceState.EXTERNAL
    d.unload()


async def test_mqtt_envelope(hass) -> None:  # noqa: ANN001
    d = make_device(hass)
    d.connection = ConnectionType.CLOUD
    d.power(100)
    d.power_since = 0.0  # the hold after the last command has passed
    d.power(200)
    (_, first), (_, second) = d.mqtt.published  # two property writes
    assert first["messageId"] == 100001 and second["messageId"] == 100002
    assert first["deviceId"] == "simdev" and "timestamp" in first
    assert d.power(700) == 0  # nothing measured yet
    d.power_actual = 650
    assert d.power(700) == 650
    d.unload()


async def test_hyper_publishes_hemsep(hass) -> None:  # noqa: ANN001
    # the Hyper is steered over hemsEP: RAM on the device, no flash wear like deviceAutomation
    d = make_device(hass, "Hyper 2000", ZendureHyper, pwr_min=-1200)
    d.connection = ConnectionType.LOCAL
    d.power(600)
    topic, cmd = d.mqtt.published[0]
    assert topic == "iot/gDa3tb/simdev/properties/write" and cmd["properties"] == {"hemsState": 1}  # raised once
    _, cmd = d.mqtt.published[1]
    assert cmd["function"] == "hemsEP" and cmd["arguments"]["outputPower"] == 600
    d.power_since = 0.0  # the hold after the last command has passed
    d.power(-400)
    _, cmd = d.mqtt.published[2]
    assert cmd["function"] == "hemsEP" and cmd["arguments"]["chargePower"] == 400  # no second hemsState write
    d.unload()


async def test_power_offline_does_nothing(hass) -> None:  # noqa: ANN001
    d = make_device(hass)
    d.unload()  # stop the loop so the queue stays inspectable
    d.connection = ConnectionType.OFFLINE
    assert d.power(500) == 0
    d.power_off()
    assert d.mqtt.published == [] and d.device_queue.empty()


async def test_power_zensdk_and_ble_queue_http_payloads(hass) -> None:  # noqa: ANN001
    d = make_device(hass)
    d.unload()
    for connection in (ConnectionType.ZENSDK, ConnectionType.BLE):
        d.connection = connection
        for watts in (700, -500, 0):
            d.power_since = 0.0  # the hold after the last command has passed
            d.power(watts)
        assert d.device_queue.get_nowait() == (Command.POWER, _limits(700))
        assert d.device_queue.get_nowait() == (Command.POWER, _limits(-500))
        assert d.device_queue.get_nowait() == (Command.POWER, _limits(0))
    assert d.mqtt.published == []


async def test_entity_write_routes_per_connection(hass) -> None:  # noqa: ANN001
    d = make_device(hass)
    d.connection = ConnectionType.LOCAL
    d.entity_write(d.entities["minSoc"], 150)
    topic, cmd = d.mqtt.published[0]
    assert topic == "iot/gDa3tb/simdev/properties/write"
    assert cmd["properties"] == {"minSoc": 150}
    d.unload()
    d.connection = ConnectionType.ZENSDK
    d.entity_write(d.entities["soc_min"], 150)
    assert d.device_queue.get_nowait() == (Command.WRITE, {"properties": {"minSoc": 150}})


async def test_zensdk_full_path(hass, monkeypatch) -> None:  # noqa: ANN001
    d = make_device(hass)
    d.connection = ConnectionType.ZENSDK
    d.ip = "1.2.3.4"
    d._msg_update = time.time()
    session = FakeSession({"properties": {"outputHomePower": 700, "gridInputPower": 0, "electricLevel": 55}})
    monkeypatch.setattr(solarflow_module, "async_get_clientsession", lambda hass: session)

    d.power(700)
    assert await wait_for(lambda: d.state == DeviceState.SENT)
    assert await wait_for(lambda: d.state == DeviceState.ACTIVE)
    assert d.power_actual == 700 and d.power_setpoint == 700
    assert session.posts[0] == ("http://1.2.3.4/properties/write", json.dumps({**_limits(700), "sn": "AB3CD9", "id": 100001}))
    assert d.soc == 55
    d.unload()


async def test_learned_ip_is_reported_for_persistence(hass) -> None:  # noqa: ANN001
    d = make_device(hass)
    changed: list = []
    d.on_config_changed = changed.append
    d.update_properties({"ip": "192.168.1.20", "properties": {}}, time.time(), True)
    await hass.async_block_till_done()  # the report lands on the loop, where the coordinator persists
    assert d.ip == "192.168.1.20" and changed == [d]
    assert d.subentry_data() == {"product": "Solarflow 2400 AC", "sn": "AB3CD9", "device_id": "simdev", "prodkey": "gDa3tb", "ip": "192.168.1.20", "blemac": "", "batteries": {}}
    d.unload()


async def test_legacy_publishes_device_automation(hass) -> None:  # noqa: ANN001
    d = make_device(hass, "Ace 1500", ZendureLegacy, pwr_min=-900)
    d.connection = ConnectionType.CLOUD
    d.power(600)
    topic, cmd = d.mqtt.published[0]
    assert topic == "iot/gDa3tb/simdev/function/invoke" and cmd["function"] == "deviceAutomation" and cmd["deviceKey"] == "simdev"
    assert cmd["arguments"] == [{"autoModelProgram": 2, "autoModelValue": {"chargingType": 0, "chargingPower": 0, "freq": 0, "outPower": 600}, "msgType": 1, "autoModel": 8}]
    d.power_since = 0.0  # the hold after the last command has passed
    d.power(-400)
    _, cmd = d.mqtt.published[1]
    assert cmd["arguments"][0]["autoModelValue"] == {"chargingType": 1, "chargingPower": 400, "freq": 0, "outPower": 0}
    d.power_off()
    _, cmd = d.mqtt.published[2]
    assert cmd["arguments"] == [{"autoModelProgram": 0, "autoModelValue": {"chargingType": 0, "chargingPower": 0, "freq": 0, "outPower": 0}, "msgType": 1, "autoModel": 0}]
    _, cmd = d.mqtt.published[3]
    assert cmd["properties"] == {"hemsState": 0}, "power off lowers the hems flag on every model"
    assert not any(c.get("function") == "hemsEP" for _, c in d.mqtt.published), "a legacy model never takes a hemsEP command"
    d.unload()


async def test_hub_sends_a_bare_number_and_never_charges(hass) -> None:  # noqa: ANN001
    d = make_device(hass, "Hub 1200", ZendureHub, pwr_min=0)
    d.connection = ConnectionType.LOCAL
    d.power(500)
    _, cmd = d.mqtt.published[0]
    assert cmd["arguments"] == [{"autoModelProgram": 2, "autoModelValue": 500, "msgType": 1, "autoModel": 8}]
    d.power_since = 0.0  # the hold after the last command has passed
    d.power(-300)  # clamped to the 0 W charge profile: the Hub has no AC input
    _, cmd = d.mqtt.published[1]
    assert cmd["arguments"][0]["autoModelValue"] == 0 and d.power_setpoint == 0
    d.unload()


async def test_limit_sliders_write_the_device_property_and_follow_the_app_limits(hass) -> None:  # noqa: ANN001
    d = make_device(hass)
    d.connection = ConnectionType.LOCAL
    out, inp = d.entities["output_limit"], d.entities["input_limit"]
    assert (out.native_max_value, inp.native_max_value) == (2400, 2400)
    await out.async_set_native_value(600)
    topic, cmd = d.mqtt.published[0]
    assert topic == "iot/gDa3tb/simdev/properties/write" and cmd["properties"] == {"outputLimit": 600}
    assert out.state == 600, "optimistic: the device echoes the property later"
    d.update_properties({"properties": {"inverseMaxPower": 800, "chargeMaxLimit": 1000, "outputLimit": 450}}, time.time(), True)
    assert (out.native_max_value, inp.native_max_value, out.state) == (800, 1000, 450)
    d.unload()


async def test_select_and_switch_without_callback_write_the_device(hass) -> None:  # noqa: ANN001
    d = make_device(hass)
    d.connection = ConnectionType.LOCAL
    d.update_properties({"properties": {"gridReverse": 0, "lampSwitch": 0}}, time.time(), True)
    for key in ("gridReverse", "lampSwitch"):
        d.entities[key].async_write_ha_state = lambda: None  # not on a platform in this test
    try:
        await d.entities["gridReverse"].async_select_option("forbidden")
        await d.entities["lampSwitch"].async_turn_on()
        assert [c["properties"] for _, c in d.mqtt.published] == [{"gridReverse": 2}, {"lampSwitch": 1}]
    finally:
        d.unload()


async def test_bypass_adds_the_pass_through_to_the_absolute_command(hass) -> None:  # noqa: ANN001
    # outputPower is absolute: a device already passing 400 W ignores a 350 W command, so the pass-through goes on top
    d = make_device(hass)
    d.connection = ConnectionType.LOCAL
    d.bypass, d.solar = True, 400
    d.power(350)
    _, cmd = d.mqtt.published[0]
    assert cmd["properties"]["outputLimit"] == 750
    assert d.power_setpoint == 750, "the published setpoint is what was sent, so the comparison with power_actual holds"
    d.unload()


async def test_bypass_never_lifts_a_charge_command(hass) -> None:  # noqa: ANN001
    d = make_device(hass)
    d.connection = ConnectionType.LOCAL
    d.bypass, d.solar = True, 400
    d.power(-500)
    _, cmd = d.mqtt.published[0]
    assert cmd["properties"] == {"smartMode": 1, "acMode": 1, "outputLimit": 0, "inputLimit": 500}
    d.unload()


async def test_bypass_command_stays_within_the_rating(hass) -> None:  # noqa: ANN001
    d = make_device(hass)
    d.connection = ConnectionType.LOCAL
    d.bypass, d.solar = True, 800
    d.power(2000)
    _, cmd = d.mqtt.published[0]
    assert cmd["properties"]["outputLimit"] == 2400, "2000 + 800 would exceed the 2400 W rating"
    d.unload()
