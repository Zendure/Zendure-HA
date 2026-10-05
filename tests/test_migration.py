"""A 1.x entry becomes a v2 entry: settings in sections, a subentry per device, every entity on its new name and key."""

from homeassistant.core import State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache

from custom_components.zendure_ha.const import DOMAIN
from custom_components.zendure_ha.migration import DEVICE_KEYS, async_migrate

OLD_DATA = {"token": "dG9rZW4=", "p1meter": "sensor.p1_power", "mqttlog": True, "mqttlocal": True, "mqttserver": "10.0.0.5", "mqttport": 1883, "mqttuser": "zen", "mqttpsw": "secret", "wifissid": "home", "wifipsw": "wifi"}


def add_entity(hass, entry, device, domain: str, key: str, name: str) -> str:  # noqa: ANN001
    # a 1.x entity: unique_id and object_id snakecase(name_key), translation_key the snakecased vendor property
    uid = f"{name}_{key}"
    return er.async_get(hass).async_get_or_create(domain, DOMAIN, uid, suggested_object_id=uid, config_entry=entry, device_id=device.id, translation_key=key).entity_id


async def test_migration(hass) -> None:  # noqa: ANN001
    entry = MockConfigEntry(domain=DOMAIN, title="Zendure", unique_id="Zendure", version=1, minor_version=8, data=OLD_DATA)
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    p1 = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("dsmr", "meter")}, name="P1")
    er.async_get(hass).async_get_or_create("sensor", "dsmr", "p1_power", suggested_object_id="p1_power", device_id=p1.id)

    manager = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "Zendure Manager"), (DOMAIN, "")}, name="Zendure Manager")
    sf = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "9RUB1mW7"), (DOMAIN, "WOB1NHMAEA88")}, name="SolarFlow 800 Pro", manufacturer="Zendure", model="SolarFlow 800 Pro", model_id="R3mn8U", serial_number="WOB1NHMAEA88", hw_version="9RUB1mW7", connections={(dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:88")})
    pack = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "CO1F45678912")}, name="AB2000S 78912", manufacturer="Zendure", model="AB2000S", serial_number="CO1F45678912", via_device=(DOMAIN, "9RUB1mW7"))
    unknown = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "xdev"), (DOMAIN, "XSN")}, name="Mystery", manufacturer="Zendure", model="SuperBase V6400", model_id="zzzzzz", serial_number="XSN", hw_version="xdev")

    ids = {}
    for domain, key in (("sensor", "outputHomePower"), ("sensor", "electricLevel"), ("sensor", "aggrOutputHome"), ("number", "minSoc"), ("number", "outputLimit"), ("select", "fuseGroup"), ("sensor", "hyperTmp"), ("sensor", "acOutputPower"), ("binary_sensor", "heatState")):
        ids[key] = add_entity(hass, entry, sf, domain, key, "solarflow_800_pro")
    ids["socLevel"] = add_entity(hass, entry, pack, "sensor", "socLevel", "ab2000s_78912")
    ids["deltaVoltage"] = add_entity(hass, entry, pack, "sensor", "deltaVoltage", "ab2000s_78912")
    ids["operation"] = add_entity(hass, entry, manager, "select", "operation", "zendure_manager")
    ids["power"] = add_entity(hass, entry, manager, "sensor", "power", "zendure_manager")
    ids["xdev"] = add_entity(hass, entry, unknown, "sensor", "electricLevel", "mystery")
    # the 1.x entities wrote snakecase translation keys
    for eid in ids.values():
        e = er.async_get(hass).async_get(eid)
        er.async_get(hass).async_update_entity(eid, translation_key="".join(f"_{c.lower()}" if c.isupper() else c for c in e.translation_key).strip("_"))
    mock_restore_cache(hass, [State(ids["aggrOutputHome"], "123.45"), State(ids["minSoc"], "10"), State(ids["operation"], "smart")])

    await async_migrate(hass, entry)
    await hass.async_block_till_done()

    assert entry.version == 2 and entry.title == "Zendure"
    assert entry.data == {"distribution": {"max_kw": 17.0, "mqtt_log": True, "p1_device": p1.id}, "zendure": {"apptoken": "dG9rZW4=", "mqtt_host": "10.0.0.5", "mqtt_port": 1883, "mqtt_user": "zen", "mqtt_psw": "secret", "wifi_ssid": "home", "wifi_psw": "wifi"}, "zeroconf": {}}

    assert [s.title for s in entry.subentries.values() if s.subentry_type == "distribution"] == ["Distribution"]
    subs = [s for s in entry.subentries.values() if s.subentry_type == "zendure"]
    assert len(subs) == 1 and subs[0].title == "SF800_88"
    assert dict(subs[0].data) == {"product": "SolarFlow 800 Pro", "sn": "WOB1NHMAEA88", "device_id": "9RUB1mW7", "prodkey": "R3mn8U", "ip": "", "blemac": "AA:BB:CC:DD:EE:88", "batteries": {"CO1F45678912": "AB2000S_12"}}

    sf = devices.async_get(sf.id)
    assert sf.name == "SF800_88" and sf.identifiers == {(DOMAIN, "WOB1NHMAEA88")} and sf.serial_number is None and sf.hw_version is None
    pack = devices.async_get(pack.id)
    assert pack.name == "AB2000S_12" and pack.identifiers == {(DOMAIN, "AB2000S_45678912")}
    assert devices.async_get(manager.id).identifiers == {(DOMAIN, "Zendure")}
    assert devices.async_get(unknown.id).name == "Mystery", "an unknown product is left alone"

    reg = er.async_get(hass)
    moved = {key: reg.async_get(eid) for key, eid in ids.items() if reg.async_get(eid) is not None}
    assert reg.async_get("sensor.sf800_88_home_output").unique_id == "sf800_88_home_output"
    assert reg.async_get("sensor.sf800_88_soc") is not None and reg.async_get("sensor.sf800_88_home_output_aggr") is not None
    assert reg.async_get("number.sf800_88_soc_min") is not None and reg.async_get("sensor.sf800_88_temp") is not None
    assert reg.async_get("sensor.sf800_88_ac_output_power") is not None and reg.async_get("binary_sensor.sf800_88_heat_state") is not None
    assert reg.async_get("sensor.ab2000_s_12_soc") is not None
    assert reg.async_get("select.zendure_mode") is not None
    assert reg.async_get("number.sf800_88_output_limit") is not None
    assert reg.async_get(ids["fuseGroup"]) is None, "entities without a successor are removed"
    assert reg.async_get(ids["deltaVoltage"]) is None and reg.async_get(ids["power"]) is None
    assert reg.async_get(ids["xdev"]) is not None and "mystery" in moved["xdev"].entity_id

    payload = await Store(hass, 1, f"{DOMAIN}.devices").async_load()
    assert payload["devices"]["WOB1NHMAEA88"] == {"sf800_88_home_output_aggr": 123.45, "sf800_88_soc_min": 10.0}
    assert payload["devices"]["Zendure"] == {"zendure_mode": "smart"}


def test_every_old_sensor_key_has_a_verdict() -> None:
    for old in ("output_limit", "electric_level", "aggr_output_home", "solar_power1", "dspversion", "fanspeed", "hyper_tmp", "inverse_max_power", "grid_off_power"):
        assert old in DEVICE_KEYS
    assert DEVICE_KEYS["dspversion"] == "dsp_version" and DEVICE_KEYS["fanspeed"] == "fan_speed" and DEVICE_KEYS["solar_power1"] == "solar_power1"
