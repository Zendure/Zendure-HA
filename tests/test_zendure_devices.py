"""Smart meter telemetry parsing and the battery pack table."""

import json
from types import SimpleNamespace

import pytest

from custom_components.zendure_ha.distribution import Distribution
from custom_components.zendure_ha.zendure.battery import ZendureBattery, battery_type
from custom_components.zendure_ha.zendure.device import ConnectionType
from custom_components.zendure_ha.zendure.driver import ZendureDriver
from custom_components.zendure_ha.zendure.smartmeter import ZendureSmartMeter

# the two reports a 3CT sent on 2026-09-23 (from a user's debug log), identity, address and wifi replaced by dummies
INFO_3CT = {
    "messageId": 102628, "timestamp": 1790198923, "deviceId": "meter3ct", "productKey": "C3yT68", "deviceSn": "TE31NJN2N000001", "product": "smartMeter3CT",
    "ip": "192.168.1.32", "wifiName": "wifi", "mac": "00:00:00:00:00:01", "blemac": "00:00:00:00:00:02", "properties": {},
}
REPORT_3CT = {
    "timestamp": 1790198923, "messageId": 102629, "deviceId": "meter3ct", "productKey": "C3yT68", "version": 2, "product": "smartMeter3CT",
    "properties": {
        "dataReady": 1, "writeRsp": 0, "lampSwitch": 1, "Iotstate": 2, "factoryModeState": 0, "OTAState": 0, "LCNState": 1, "bindstate": 0, "ts": 1790198765,
        "tsZone": 14, "set_scene": 1, "rssi": -56, "localAPIEnable": True, "phaseMode": 1,
    },
    "circuits": [
        {"u": 22542, "i": 36, "pf": 0.5329999923706055, "p": 44, "sub_u": 22550, "sub_i": 0, "sub_pf": 1, "sub_p": 0, "phase": 1, "type": 0, "sub_type": 0, "i_rev": 0},
        {"u": 22506, "i": 51, "pf": 0.6729999780654907, "p": 81, "sub_u": 22512, "sub_i": 0, "sub_pf": 1, "sub_p": 0, "phase": 2, "type": 0, "sub_type": 0, "i_rev": 0},
        {"u": 22548, "i": 89, "pf": 0.4339999854564667, "p": 87, "sub_u": 22559, "sub_i": 0, "sub_pf": 1, "sub_p": 0, "phase": 3, "type": 0, "sub_type": 0, "i_rev": 0},
    ],
}


async def test_smartmeter_properties_and_circuits(hass) -> None:  # noqa: ANN001
    d = ZendureSmartMeter(hass, None, None, "MeterP1_D9", {"prodkey": "q331b1", "device_id": "meterdev", "sn": "SM3CD9"}, "SmartMeter P1")
    d.update_properties({"properties": {"totalPower": 2345, "totalActiveEnergy": 12345, "totalReverseEnergy": 6789, "rssi": -60}}, 0.0)
    assert d.totalPower == 2345
    assert d.entities["totalImport"].state == pytest.approx(12.345)  # Wh -> kWh
    assert d.entities["totalExport"].state == pytest.approx(6.789)
    assert d.entities["rssi"].state == -60
    assert d.entities["totalPower"].unique_id == "meter_p1_d9_total_power"

    d.update_properties({"circuits": [{"phase": 1, "p": 100, "i": 150, "u": 23012}, {"phase": 2, "p": -50, "i": 25, "u": 22990}, {"phase": 3}, {}]}, 0.0)
    assert d.phase1_power == 100
    assert d.phase1_current == pytest.approx(1.5)  # amps*100 -> A
    assert d.phase1_voltage == pytest.approx(230.12)  # volts*100 -> V
    assert d.phase2_power == -50
    assert d.phase3_power == 0.0
    assert d.ble_name == "ZenS3C_9", "smart meters use the SolarFlow shape"
    d.unload()


async def test_smartmeter_3ct_total_is_the_sum_of_its_phases(hass) -> None:  # noqa: ANN001
    d = ZendureSmartMeter(hass, None, None, "Meter3CT_01", {"prodkey": "c3yt68", "device_id": "meter3ct", "sn": "TE31NJN2N000001"}, "smartMeter3CT")

    # a 3CT reports no totalPower: the total is the sum of its three phases
    d.update_properties(REPORT_3CT, 0.0)
    assert d.totalPower == 212
    assert d.phase1_power == 44 and d.phase2_power == 81 and d.phase3_power == 87
    assert d.phase1_current == pytest.approx(0.36) and d.phase1_voltage == pytest.approx(225.42)

    # a report that carries totalPower keeps it, and circuits without power leave the total alone
    d.update_properties({"properties": {"totalPower": 300}, "circuits": [{"phase": 1, "p": 100}]}, 0.0)
    assert d.totalPower == 300
    d.update_properties({"circuits": [{"phase": 1, "u": 23000}]}, 0.0)
    assert d.totalPower == 300
    d.unload()


async def test_a_3ct_is_discovered_on_its_device_sn(hass, entry) -> None:  # noqa: ANN001
    # the 3CT names its serial deviceSn; the product comes from the payload, the prodkey and deviceId from the topic
    driver = ZendureDriver(hass, entry.entry_id, [], Distribution(hass, entry.entry_id))
    found: list[dict] = []
    driver.on_discovered = found.append
    driver._on_mqtt_message(None, ConnectionType.LOCAL, SimpleNamespace(topic="/C3yT68/meter3ct/properties/report", payload=json.dumps(INFO_3CT).encode()))
    await hass.async_block_till_done()
    assert len(found) == 1
    assert found[0]["sn"] == "TE31NJN2N000001" and found[0]["product"] == "smartMeter3CT" and found[0]["family"] == "Meter3CT"
    assert found[0]["device_id"] == "meter3ct" and found[0]["prodkey"] == "C3yT68"


@pytest.mark.parametrize(
    ("sn", "pack_type", "model", "kwh"),
    [
        ("AO13456789", None, "AIO2400", 2.4),
        ("AO10456789", None, "AB1000", 0.96),
        ("BO10456789", None, "AB1000S", 0.96),
        ("BO10456789", 70, "I8000", 8.0),
        ("CO1F456789", None, "AB2000S", 1.92),
        ("CO1E456789", None, "AB2000X", 1.92),
        ("CO10456789", None, "AB2000", 1.92),
        ("FO10456789", None, "AB3000", 2.88),
        ("GO10456789", None, "AB3000L", 2.88),
        ("JO2A456789", None, "I2400", 2.4),
        ("XO10456789", None, "Unknown", 0.0),
    ],
)
def test_battery_table(sn: str, pack_type: int | None, model: str, kwh: float) -> None:
    assert battery_type(sn, pack_type) == (model, kwh)


async def test_battery_names_are_fixed_in_the_parent(hass) -> None:  # noqa: ANN001
    changed: list = []
    parent = SimpleNamespace(driver=None, subentry_id=None, device_id="parent", battery_names={}, config_changed=lambda: changed.append(1))
    first = ZendureBattery(hass, "CO1F45678912", parent)
    second = ZendureBattery(hass, "CO1F45678812", parent)  # same two-character tail: the newcomer grows to three
    assert (first.name, second.name) == ("AB2000S_12", "AB2000S_812")
    assert parent.battery_names == {"CO1F45678912": "AB2000S_12", "CO1F45678812": "AB2000S_812"} and len(changed) == 2
    assert first.device_id == "AB2000S_45678912" and first.entities["soc"].unique_id == "ab2000_s_12_soc"
    ZendureBattery._registry.clear()
    again = ZendureBattery(hass, "CO1F45678812", parent)
    assert again.name == "AB2000S_812", "a known pack keeps the name the parent remembers"
    ZendureBattery._registry.clear()
