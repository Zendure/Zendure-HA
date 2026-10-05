"""Every entity the devices create has a translation, and every translation file carries the same keys as strings.json."""

import json
from pathlib import Path

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity

from custom_components.zendure_ha.distribution import Distribution
from custom_components.zendure_ha.zendure.battery import ZendureBattery
from custom_components.zendure_ha.zendure.smartmeter import ZendureSmartMeter
from custom_components.zendure_ha.zendure.solarflow import ZendureSolarFlow

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "zendure_ha"
PLATFORMS = ((SensorEntity, "sensor"), (NumberEntity, "number"), (SelectEntity, "select"), (SwitchEntity, "switch"), (BinarySensorEntity, "binary_sensor"), (ButtonEntity, "button"))


def platform(entity) -> str:  # noqa: ANN001
    return next(name for cls, name in PLATFORMS if isinstance(entity, cls))


async def test_every_entity_has_a_translation(hass, entry) -> None:  # noqa: ANN001
    strings = json.loads((PACKAGE / "strings.json").read_text(encoding="utf-8"))["entity"]
    sf = ZendureSolarFlow(hass, None, None, "SF800_88", {"prodkey": "a4ss5P", "device_id": "d1", "sn": "WOB1NHMAEA88"}, "Solarflow 800", 800, -1000, 0)
    for vendor, (cls, name, kwargs) in ZendureSolarFlow.SENSORS.items():
        if vendor not in sf.entities:
            cls(sf, name, vendor_id=vendor, **kwargs)
    devices = [Distribution(hass, entry.entry_id), sf, ZendureSmartMeter(hass, None, None, "MeterP1_61", {"prodkey": "q331b1", "device_id": "m1", "sn": "SOP1Q331P61"}, "SmartMeter P1"), ZendureBattery(hass, "CO1F45678912", sf)]
    missing = set()
    for device in devices:
        for entity in {id(e): e for e in device.entities.values() if e is not None}.values():
            if entity.translation_key not in strings.get(platform(entity), {}):
                missing.add((platform(entity), entity.translation_key))
    sf.unload()
    ZendureBattery._registry.clear()
    assert not missing, sorted(missing)


def test_translation_files_mirror_strings() -> None:
    def keys(node, prefix=""):
        return {f"{prefix}{k}" for k, v in node.items() if not isinstance(v, dict)} | {k2 for k, v in node.items() if isinstance(v, dict) for k2 in keys(v, f"{prefix}{k}.")}

    strings = json.loads((PACKAGE / "strings.json").read_text(encoding="utf-8"))
    for path in sorted((PACKAGE / "translations").glob("*.json")):
        assert keys(json.loads(path.read_text(encoding="utf-8"))) == keys(strings), path.name
