"""Device names: family plus the shortest unique serial tail, fixed at creation; every id follows that name."""

from custom_components.zendure_ha.entity import HaEntity, EntityDevice, snakecase
from custom_components.zendure_ha.sensor import HaSensor
from custom_components.zendure_ha.entity import unique_name
from custom_components.zendure_ha.zendure.driver import ZendureDriver
from custom_components.zendure_ha.zendure.driver import PRODUCTS
from custom_components.zendure_ha.zendure.solarflow import ZendureSolarFlow


def test_unique_name_takes_the_shortest_free_tail() -> None:
    assert unique_name("SF800", "WOB1NHMAMXXXX88", set()) == "SF800_88"
    assert unique_name("SF800", "WOB1NHMAMXXXX90", {"SF800_88"}) == "SF800_90"
    # the same two characters are taken: the newcomer grows, the existing device keeps its name
    assert unique_name("SF800", "WOB1NHMAMXXXY88", {"SF800_88"}) == "SF800_Y88"
    assert unique_name("SF800", "AB", {"SF800_AB"}) == "SF800_AB", "nothing longer is left: the full serial is the fallback"


def test_jans_fleet_names() -> None:
    fleet = [("SF800", "WOB1NHMAEA88"), ("SF800", "WOB1NHMAXB90"), ("SF800", "WOB1NHMAXB50"), ("SF2400", "HOA1BC8BHA37"), ("SF2400", "HOB1BC8BHB12"), ("SF2400", "HOC1BC8BHC45"), ("SF1600", "HOD1651HD62"), ("Hyper2000", "EOK1GDA3K22"), ("MeterP1", "SOP1Q331P61")]
    taken: set[str] = set()
    names = []
    for family, sn in fleet:
        names.append(unique_name(family, sn, taken))
        taken.add(names[-1])
    assert names == ["SF800_88", "SF800_90", "SF800_50", "SF2400_37", "SF2400_12", "SF2400_45", "SF1600_62", "Hyper2000_22", "MeterP1_61"]


def test_every_product_has_a_family() -> None:
    for product, row in PRODUCTS.items():
        assert product == product.lower(), f"{product}: keys are lowercase"
        assert isinstance(row.family, str) and row.family and " " not in row.family, f"{product}: family {row.family!r}"
        assert (row.pwr_max != 0) == issubclass(row.cls, ZendureSolarFlow), f"{product}: battery rows carry power, meter rows none"


def test_driver_reports_the_family_from_the_product_only() -> None:
    zendure = ZendureDriver
    assert zendure.device_family({"product": "Hyper 2000", "prodkey": "nope"}) == "Hyper2000"
    assert zendure.device_family({"product": "nope", "prodkey": "gDa3tb"}) is None


async def test_entity_ids_follow_the_device_name(hass) -> None:  # noqa: ANN001
    class Device(EntityDevice):
        def entity_create(self) -> None:
            HaSensor(self, "home_output", "outputHomePower", uom="W")

    device = Device(hass, "9RUB1mW7", "SF800_88")
    entity: HaEntity = device.entities["home_output"]
    assert entity is device.entities["outputHomePower"], "reachable by display key and by vendor id"
    assert entity.unique_id == "sf800_88_home_output"
    assert entity.internal_integration_suggested_object_id == "sf800_88_home_output"
    assert entity.translation_key == "home_output"
    assert snakecase("Solarflow 2400 AC+") == "solarflow_2400_acplus"
