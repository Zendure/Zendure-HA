"""Config flow: the settings form in sections, discovery that seeds or feeds it, and the device subentries."""

from ipaddress import ip_address
import pytest
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from custom_components.zendure_ha.config_flow import SETTINGS_SCHEMA, flat, nested
from custom_components.zendure_ha.const import DOMAIN
from custom_components.zendure_ha.zendure.driver import ZendureDriver

from helpers import ble_info, subentry

@pytest.fixture(autouse=True)
def _dependencies(hass, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    # loading the flow handler would set up the manifest dependencies; a created entry sets up for real, without a broker
    for dep in ("recorder", "bluetooth", "zeroconf"):
        hass.config.components.add(dep)

    async def connect(self) -> None:  # noqa: ANN001
        return

    monkeypatch.setattr(ZendureDriver, "connect", connect)


SETTINGS = {"distribution": {"max_kw": 12.0, "max_output": 800, "mqtt_log": True}, "cloud": {"apptoken": "abc"}, "local_broker": {"mqtt_host": "10.0.0.5", "mqtt_port": 1883}, "wifi": {}}


def zeroconf_info(host: str = "192.168.1.50", name: str = "zendure-SolarFlow800Pro-WOB1NHMAEA88._http._tcp.local.") -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(ip_address=ip_address(host), ip_addresses=[ip_address(host)], hostname=name.split("._")[0] + ".local.", name=name, port=80, properties={}, type="_http._tcp.local.")


def test_schema_sections_and_round_trip() -> None:
    assert list(SETTINGS_SCHEMA.schema) == ["distribution", "cloud", "local_broker", "wifi"]
    data = flat(SETTINGS)
    assert data == {"distribution": {"max_kw": 12.0, "max_output": 800, "mqtt_log": True}, "zendure": {"apptoken": "abc", "mqtt_host": "10.0.0.5", "mqtt_port": 1883}}
    assert nested(data) == {k: v for k, v in SETTINGS.items() if v}


async def test_user_flow_creates_the_single_entry(hass) -> None:  # noqa: ANN001
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SETTINGS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {**flat(SETTINGS), "zeroconf": {}}
    assert result["result"].unique_id == "Zendure"

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "single_instance_allowed"


def test_zeroconf_matcher_reads_the_hostname() -> None:
    assert ZendureDriver.match_zeroconf(zeroconf_info()) == {"sn": "WOB1NHMAEA88", "model": "SolarFlow800Pro", "ip": "192.168.1.50"}
    assert ZendureDriver.match_zeroconf(zeroconf_info(name="Zendure-SolarFlow-2400-AC-Plus-HOA1BC8B37._zendure._tcp.local.")) == {"sn": "HOA1BC8B37", "model": "SolarFlow-2400-AC-Plus", "ip": "192.168.1.50"}
    assert ZendureDriver.match_zeroconf(zeroconf_info(name="shelly-abc._http._tcp.local.")) is None


async def test_zeroconf_before_setup_seeds_the_entry(hass, monkeypatch) -> None:  # noqa: ANN001
    async def probe(cls, hass, ip):  # noqa: ANN001, ANN202
        return None

    # no answer from the device: its ip waits in the entry until the device shows up
    monkeypatch.setattr(ZendureDriver, "probe", classmethod(probe))
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info())
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SETTINGS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["zeroconf"] == {"WOB1NHMAEA88": "192.168.1.50"}, "the find waits in the entry until the device shows up"


async def test_discovery_with_an_entry_is_aborted_by_ha(hass, entry) -> None:  # noqa: ANN001
    # single_config_entry: HA never reaches our steps once the entry exists, the driver listens itself from then on
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info())
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "single_instance_allowed"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=ble_info("ZenWB1_8", "AA:BB:CC:DD:EE:08"))
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "single_instance_allowed"


async def test_bluetooth_before_setup_offers_the_form(hass) -> None:  # noqa: ANN001
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=ble_info("ZenWB1_8", "AA:BB:CC:DD:EE:08"))
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"


async def test_reconfigure_keeps_the_rest_of_the_data(hass, entry) -> None:  # noqa: ANN001
    entry.mock_state(hass, entry.state)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SETTINGS)
    await hass.async_block_till_done()  # the abort schedules a reload; let it finish inside the test
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert entry.data["distribution"] == {"max_kw": 12.0, "max_output": 800, "mqtt_log": True} and entry.data["zendure"]["mqtt_host"] == "10.0.0.5"


async def test_manual_device_subentry(hass, entry) -> None:  # noqa: ANN001
    entry.mock_state(hass, entry.state)
    result = await hass.config_entries.subentries.async_init((entry.entry_id, "zendure"), context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"sn": "S1", "product": "nope", "device_id": "d1"})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"product": "unknown_product"}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"sn": "WOB1NHMAEA88", "product": "solarFlow800", "device_id": "d1", "prodkey": "a4ss5P", "ip": "192.168.1.60"})
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["title"] == "SF800_88"
    assert dict(result["data"]) == {"sn": "WOB1NHMAEA88", "product": "solarFlow800", "device_id": "d1", "prodkey": "a4ss5P", "ip": "192.168.1.60", "blemac": ""}

    result = await hass.config_entries.subentries.async_init((entry.entry_id, "zendure"), context={"source": SOURCE_USER})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"sn": "WOB1NHMAEA88", "product": "solarFlow800", "device_id": "d2"})
    assert result["errors"] == {"sn": "device_exists"}


async def test_reconfigure_device_subentry_edits_ip_and_address(hass) -> None:  # noqa: ANN001
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, title="Zendure", unique_id="Zendure", version=2, data={"distribution": {}, "zendure": {}}, subentries_data=[subentry({"prodkey": "a4ss5P", "device_id": "d1", "sn": "WOB1NHMAEA88", "ip": "", "blemac": ""}, "SF800_88")])
    entry.add_to_hass(hass)
    entry.mock_state(hass, entry.state)
    sub = next(iter(entry.subentries.values()))
    result = await hass.config_entries.subentries.async_init((entry.entry_id, "zendure"), context={"source": "reconfigure", "subentry_id": sub.subentry_id})
    assert result["type"] is FlowResultType.FORM and set(result["data_schema"].schema) == {"ip", "blemac"}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"ip": "192.168.1.70", "blemac": "AA:BB:CC:DD:EE:88"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert dict(entry.subentries[sub.subentry_id].data) == {"prodkey": "a4ss5P", "device_id": "d1", "sn": "WOB1NHMAEA88", "ip": "192.168.1.70", "blemac": "AA:BB:CC:DD:EE:88"}
    assert entry.subentries[sub.subentry_id].title == "SF800_88", "the name never changes"


async def test_zeroconf_before_setup_creates_the_device_when_it_answers(hass, monkeypatch) -> None:  # noqa: ANN001
    async def probe(cls, hass, ip):  # noqa: ANN001, ANN202
        return {"prodkey": "R3mn8U", "device_id": "9RUB1mW7", "sn": "WOB1NHMAEA88", "product": "solarFlow800Pro", "ip": ip}

    monkeypatch.setattr(ZendureDriver, "probe", classmethod(probe))
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info())
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SETTINGS)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["data"]["zeroconf"] == {}
    assert [s.title for s in result["result"].subentries.values() if s.subentry_type == "distribution"] == ["Distribution"]
    subs = [s for s in result["result"].subentries.values() if s.subentry_type == "zendure"]
    assert len(subs) == 1 and subs[0].title == "SF800_88" and dict(subs[0].data) == {"prodkey": "R3mn8U", "device_id": "9RUB1mW7", "sn": "WOB1NHMAEA88", "product": "solarFlow800Pro", "ip": "192.168.1.50", "blemac": ""}


async def test_bluetooth_before_setup_creates_the_device_when_it_answers(hass, monkeypatch) -> None:  # noqa: ANN001
    async def probe_ble(cls, hass, address):  # noqa: ANN001, ANN202
        return {"prodkey": "gDa3tb", "product": "hyper2000", "device_id": "hyp1", "sn": "EOK1GDA3K22", "blemac": address}

    monkeypatch.setattr(ZendureDriver, "probe_ble", classmethod(probe_ble))
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=ble_info("ZenEK_22", "AA:BB:CC:DD:EE:22"))
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SETTINGS)
    await hass.async_block_till_done()
    subs = [s for s in result["result"].subentries.values() if s.subentry_type == "zendure"]
    assert len(subs) == 1 and subs[0].title == "Hyper2000_22" and dict(subs[0].data) == {"prodkey": "gDa3tb", "product": "hyper2000", "device_id": "hyp1", "sn": "EOK1GDA3K22", "ip": "", "blemac": "AA:BB:CC:DD:EE:22"}
