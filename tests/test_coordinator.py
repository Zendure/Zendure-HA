"""Coordinator: devices mirror the subentries, discovery creates them with a fixed name, learned addresses are persisted."""

import pytest
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zendure_ha.const import DOMAIN
from custom_components.zendure_ha.coordinator import Coordinator
from custom_components.zendure_ha.zendure.driver import ZendureDriver
from custom_components.zendure_ha.zendure.solarflow import ZendureHub, ZendureHyper

from helpers import ble_info, subentry

SF = {"product": "Hyper 2000", "sn": "EOK1GDA3K22", "device_id": "dev1", "prodkey": "gDa3tb", "ip": "", "blemac": ""}


@pytest.fixture(autouse=True)
def _offline_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    # no broker, no cloud: the driver only builds and routes in these tests
    async def connect(self) -> None:  # noqa: ANN001
        return

    monkeypatch.setattr(ZendureDriver, "connect", connect)


async def setup(hass, *subs: dict, data: dict | None = None) -> tuple[Coordinator, MockConfigEntry]:  # noqa: ANN001
    entry = MockConfigEntry(domain=DOMAIN, title="Zendure", unique_id="Zendure", version=2, data=data or {"distribution": {"max_kw": 12.0, "max_output": 800, "mqtt_log": False}, "zendure": {"mqtt_host": "10.0.0.5"}}, subentries_data=list(subs))
    entry.add_to_hass(hass)
    coord = Coordinator(hass, entry)
    await coord.async_setup()
    return coord, entry


async def test_devices_are_built_from_the_subentries(hass) -> None:  # noqa: ANN001
    coord, entry = await setup(hass, subentry(SF, "Hyper2000_22"), subentry({"product": "Hub 1200", "sn": "HOA1BKTV88", "device_id": "hub1", "prodkey": "73bkTV"}, "Hub1200_88"))
    zendure = coord.zendure
    assert set(zendure.devices) == {"EOK1GDA3K22", "HOA1BKTV88"}
    hyper, hub = zendure.devices["EOK1GDA3K22"], zendure.devices["HOA1BKTV88"]
    assert type(hyper) is ZendureHyper and type(hub) is ZendureHub
    assert hyper.name == "Hyper2000_22" and hyper.subentry_id in entry.subentries
    assert hyper in coord.distribution.devices and hub in coord.distribution.devices and hyper in coord.devices
    assert coord.distribution.max_kw == 12.0 and coord.distribution.max_output == 800 and zendure.mqtt_host == "10.0.0.5", "the entry data sections landed on their objects"

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "EOK1GDA3K22")})
    assert device is not None and device.name == "Hyper2000_22" and device.model_id == "gDa3tb" and device.manufacturer == "Zendure"
    assert device.serial_number is None and device.hw_version is None, "the serial and the device key are not shown"
    assert device.config_entries_subentries[entry.entry_id] == {hyper.subentry_id}
    assert hyper.entities["soc"].unique_id == "hyper2000_22_soc"
    await coord.unload()


async def test_discovery_adds_a_subentry_and_builds_the_device(hass) -> None:  # noqa: ANN001
    coord, entry = await setup(hass, subentry(SF, "Hyper2000_22"))
    zendure = coord.zendure
    zendure.discover("EOK1GDA3M22", "Hyper 2000", "dev2", "gDa3tb")  # same two-character tail as the existing Hyper: the newcomer grows
    await hass.async_block_till_done()
    subs = {s.title: s for s in entry.subentries.values()}
    assert "Hyper2000_M22" in subs and dict(subs["Hyper2000_M22"].data) == {"sn": "EOK1GDA3M22", "product": "Hyper 2000", "device_id": "dev2", "prodkey": "gDa3tb", "ip": "", "blemac": ""}
    assert "EOK1GDA3M22" in zendure.devices and zendure.devices["EOK1GDA3M22"].name == "Hyper2000_M22"

    zendure.discover("EOK1GDA3M22", "Hyper 2000", "dev2", "gDa3tb")  # a second announcement changes nothing
    zendure.discover("X", "zzzzzz", "dev3")  # an unknown product is skipped
    await hass.async_block_till_done()
    assert len(entry.subentries) == 3  # distribution, Hyper2000_22, Hyper2000_M22
    await coord.unload()


async def test_removed_subentry_drops_the_device(hass) -> None:  # noqa: ANN001
    coord, entry = await setup(hass, subentry(SF, "Hyper2000_22"))
    device = coord.zendure.devices["EOK1GDA3K22"]
    hass.config_entries.async_remove_subentry(entry, device.subentry_id)
    await hass.async_block_till_done()
    assert "EOK1GDA3K22" not in coord.zendure.devices and device not in coord.devices and device not in coord.distribution.devices
    await coord.unload()


async def test_learned_ip_and_address_are_persisted(hass) -> None:  # noqa: ANN001
    coord, entry = await setup(hass, subentry(SF, "Hyper2000_22"))
    device = coord.zendure.devices["EOK1GDA3K22"]
    device.update_properties({"ip": "192.168.1.30", "properties": {}}, 0.0, True)
    await hass.async_block_till_done()
    assert entry.subentries[device.subentry_id].data["ip"] == "192.168.1.30"

    zendure = coord.zendure
    zendure._on_bluetooth(ble_info("ZenEK_22", "AA:BB:CC:DD:EE:22", b"A3K22"), None)
    zendure._on_bluetooth(ble_info("ZenEK_22", "11:22:33:44:55:66", b"XXXXX"), None)  # the serial tail in the manufacturer data settles a name tie
    zendure._on_bluetooth(ble_info("ZenWB1_8", "11:22:33:44:55:66"), None)
    await hass.async_block_till_done()
    assert device.blemac == "AA:BB:CC:DD:EE:22" and entry.subentries[device.subentry_id].data["blemac"] == "AA:BB:CC:DD:EE:22"
    await coord.unload()


async def test_zeroconf_ip_reaches_a_known_or_a_future_device(hass) -> None:  # noqa: ANN001
    coord, entry = await setup(hass, subentry(SF, "Hyper2000_22"))
    coord.zeroconf({"sn": "EOK1GDA3K22", "device_id": "", "ip": "192.168.1.40"})
    await hass.async_block_till_done()
    assert coord.zendure.devices["EOK1GDA3K22"].ip == "192.168.1.40"
    assert entry.subentries[coord.zendure.devices["EOK1GDA3K22"].subentry_id].data["ip"] == "192.168.1.40"

    coord.zeroconf({"sn": "WOB1NHMAEA88", "device_id": "", "ip": "192.168.1.41"})
    coord.zendure.discover("WOB1NHMAEA88", "solarFlow800", "dev5", "a4ss5P")
    await hass.async_block_till_done()
    assert coord.zendure.devices["WOB1NHMAEA88"].ip == "192.168.1.41", "an ip found before the device arrived is applied when it does"
    await coord.unload()


async def test_changed_settings_reconnect_the_driver(hass, monkeypatch) -> None:  # noqa: ANN001
    coord, entry = await setup(hass)
    connects: list[str] = []

    async def connect(self) -> None:  # noqa: ANN001
        connects.append(self.mqtt_host)

    monkeypatch.setattr(ZendureDriver, "connect", connect)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "zendure": {"mqtt_host": "10.0.0.9"}})
    await hass.async_block_till_done()
    assert connects == ["10.0.0.9"] and coord.zendure.mqtt_host == "10.0.0.9"
    await coord.unload()


class _Report:
    def __init__(self, body: dict | None) -> None:
        self.body = body

    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *args) -> None:  # noqa: ANN002
        return None

    async def json(self, content_type=None):  # noqa: ANN001, ANN201
        if self.body is None:
            raise OSError("no answer")
        return self.body


def fake_session(body: dict | None):  # noqa: ANN201
    return lambda hass: type("S", (), {"get": lambda self, url, timeout=None: _Report(body)})()


async def test_mdns_probe_creates_the_device_or_updates_its_ip(hass, monkeypatch) -> None:  # noqa: ANN001
    import custom_components.zendure_ha.zendure.driver as driver_module

    coord, entry = await setup(hass, subentry(SF, "Hyper2000_22"))
    zendure = coord.zendure

    # a new device answers with its identity: it becomes a subentry with the ip already in place
    monkeypatch.setattr(driver_module, "async_get_clientsession", fake_session({"sn": "WOB1NHMAEA88", "product": "solarFlow800", "properties": {}}))
    await zendure.zeroconf_found("zendure-SolarFlow800-WOB1NHMAEA88._http._tcp.local.", "192.168.1.90")
    await hass.async_block_till_done()
    assert "WOB1NHMAEA88" in zendure.devices and zendure.devices["WOB1NHMAEA88"].name == "SF800_88" and zendure.devices["WOB1NHMAEA88"].ip == "192.168.1.90"

    # a known device with a new address
    monkeypatch.setattr(driver_module, "async_get_clientsession", fake_session({"productKey": "gDa3tb", "sn": "EOK1GDA3K22", "deviceId": "dev1"}))
    await zendure.zeroconf_found("zendure-Hyper2000-EOK1GDA3K22._http._tcp.local.", "192.168.1.91")
    await hass.async_block_till_done()
    assert zendure.devices["EOK1GDA3K22"].ip == "192.168.1.91" and entry.subentries[zendure.devices["EOK1GDA3K22"].subentry_id].data["ip"] == "192.168.1.91"

    # no answer: the hostname carries serial and product, and that is enough to create the device
    monkeypatch.setattr(driver_module, "async_get_clientsession", fake_session(None))
    await zendure.zeroconf_found("zendure-SolarFlow800-WOB1NHMAXB90._http._tcp.local.", "192.168.1.92")
    await hass.async_block_till_done()
    assert zendure.devices["WOB1NHMAXB90"].name == "SF800_90" and zendure.devices["WOB1NHMAXB90"].ip == "192.168.1.92"

    # a report without identity falls back to the hostname the same way
    monkeypatch.setattr(driver_module, "async_get_clientsession", fake_session({"properties": {}}))
    await zendure.zeroconf_found("zendure-SolarFlow800-WOB1NHMAXB50._http._tcp.local.", "192.168.1.93")
    await hass.async_block_till_done()
    assert zendure.devices["WOB1NHMAXB50"].ip == "192.168.1.93" and len(zendure.devices) == 4
    await coord.unload()


async def test_bluetooth_stranger_is_probed_once_and_created(hass, monkeypatch) -> None:  # noqa: ANN001
    coord, entry = await setup(hass, subentry(SF, "Hyper2000_22"))
    zendure = coord.zendure
    probes: list[str] = []

    async def probe_ble(cls, hass, address):  # noqa: ANN001, ANN202
        probes.append(address)
        return {"prodkey": "a4ss5P", "device_id": "dev8", "sn": "WOB1NHMAEA88", "blemac": address} if address.endswith("88") else None

    monkeypatch.setattr(ZendureDriver, "probe_ble", classmethod(probe_ble))
    zendure._on_bluetooth(ble_info("ZenWB1_8", "AA:BB:CC:DD:EE:88"), None)
    zendure._on_bluetooth(ble_info("ZenWB1_8", "AA:BB:CC:DD:EE:88"), None)  # the same advertisement again: no second probe
    zendure._on_bluetooth(ble_info("ZenXX9_0", "AA:BB:CC:DD:EE:99"), None)  # answers nothing: no device
    await hass.async_block_till_done()
    assert probes == ["AA:BB:CC:DD:EE:88", "AA:BB:CC:DD:EE:99"]
    device = zendure.devices["WOB1NHMAEA88"]
    assert device.name == "SF800_88" and device.blemac == "AA:BB:CC:DD:EE:88"
    assert entry.subentries[device.subentry_id].data["blemac"] == "AA:BB:CC:DD:EE:88"
    assert entry.subentries[device.subentry_id].data["product"] == "solarflow800", "the handshake names a product key, the table gives the product"
    assert len(zendure.devices) == 2

    zendure._on_bluetooth(ble_info("ZenWB1_8", "AA:BB:CC:DD:EE:88"), None)  # known now: matched, not probed
    await hass.async_block_till_done()
    assert probes == ["AA:BB:CC:DD:EE:88", "AA:BB:CC:DD:EE:99"]
    await coord.unload()
