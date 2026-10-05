"""ZendureBattery: battery pack child device of a ZendureDevice, discovered through MQTT packData."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from ..const import DOMAIN
from ..entity import EntityDevice, device_by_identifier, unique_name
from ..sensor import HaSensor
from .device import centi_convert, deci_convert, temp_convert


def battery_type(sn: str, pack_type: int | None = None) -> tuple[str, float]:
    """Model and capacity from the serial prefix; packType tells the SF4000's internal pack apart from the AB1000S it shares a prefix with."""
    match sn[0]:
        case "A":
            return ("AIO2400", 2.4) if sn[3] == "3" else ("AB1000", 0.96)
        case "B":
            return ("I8000", 8.0) if pack_type == 70 else ("AB1000S", 0.96)
        case "C":
            return ("AB2000" + ("S" if sn[3] == "F" else "X" if sn[3] == "E" else ""), 1.92)
        case "F":
            return ("AB3000", 2.88)
        case "G":
            return ("AB3000L", 2.88)
        case "J":
            return ("I2400", 2.4)  # JO2A: internal pack of the SF2400 Pro, JO4A: of the SF2400 AC+
        case _:
            return ("Unknown", 0.0)


class ZendureBattery(EntityDevice):
    """Battery pack with per-cell monitoring entities."""

    _registry: dict[str, ZendureBattery] = {}

    def __init__(self, hass: HomeAssistant, sn: str, parent: Any, pack_type: int | None = None) -> None:
        ZendureBattery._registry[sn] = self
        self.sn = sn
        self.model, self.kWh = battery_type(sn, pack_type)
        device_id = f"{self.model}_{sn[-8:]}"

        # the name is fixed the first time the pack is seen and kept in the parent's subentry, like the device names
        names: dict[str, str] = getattr(parent, "battery_names", {})
        if not (name := names.get(sn)):
            driver = getattr(parent, "driver", None)
            taken = {d.name for d in driver.devices.values()} if driver is not None else set()
            name = unique_name(self.model, sn, taken | {b.name for b in ZendureBattery._registry.values() if b is not self})
            names[sn] = name
            if hasattr(parent, "config_changed"):
                parent.config_changed()

        if driver := getattr(parent, "driver", None):
            # HA >= 2026.8 deprecates the via_device identifier for via_device_id; the parent is registered in ZendureDevice.__init__, so its id is known here
            via: dict[str, Any] = {"via_device": (DOMAIN, parent.device_id)}
            if "via_device_id" in DeviceInfo.__optional_keys__:
                via = {"via_device_id": p.id} if (p := device_by_identifier(hass, parent.device_id)) is not None else {}
            dev = dr.async_get(hass).async_get_or_create(config_entry_id=driver.entry_id, config_subentry_id=parent.subentry_id, identifiers={(DOMAIN, device_id)}, name=name, manufacturer=driver.manufacturer, model=self.model, **via)

            # a pack migrated from 1.x is still a member of the main entry; take it out so it only belongs to its parent's subentry
            if parent.subentry_id and None in dev.config_entries_subentries.get(driver.entry_id, set()):
                dr.async_get(hass).async_update_device(dev.id, remove_config_entry_id=driver.entry_id, remove_config_subentry_id=None)
        super().__init__(hass, device_id, name, getattr(parent, "subentry_id", None))

    def entity_create(self) -> None:
        HaSensor(self, "packState")
        HaSensor(self, "soc", "socLevel", uom="%", device_class="battery", state_class="measurement")
        HaSensor(self, "state")
        HaSensor(self, "power", uom="W", device_class="power", state_class="measurement")
        HaSensor(self, "maxTemp", uom="°C", device_class="temperature", state_class="measurement", convert=temp_convert)
        HaSensor(self, "totalVol", uom="V", device_class="voltage", state_class="measurement")
        HaSensor(self, "batcur", uom="A", device_class="current", state_class="measurement", convert=deci_convert)
        HaSensor(self, "maxVol", uom="V", device_class="voltage", state_class="measurement", convert=centi_convert)
        HaSensor(self, "minVol", uom="V", device_class="voltage", state_class="measurement", convert=centi_convert)

    def update_properties(self, payload: dict, now: float) -> None:
        """Update battery properties from MQTT telemetry (packData entry)."""
        for key, value in payload.items():
            self.entity_update(key, value)
