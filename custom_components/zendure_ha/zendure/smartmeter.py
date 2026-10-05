"""ZendureSmartMeter: Zendure CT clamp over MQTT, reporting per-phase power, current and voltage."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import paho.mqtt.client as mqtt
from homeassistant.core import HomeAssistant

from ..entity import EntityDevice
from ..sensor import HaSensor
from .device import ConnectionType, ZendureDevice, centi_convert

if TYPE_CHECKING:
    from .driver import ZendureDriver

_LOGGER = logging.getLogger(__name__)


class ZendureSmartMeter(ZendureDevice, EntityDevice):
    """Zendure smart meter / CT clamp with per-phase monitoring."""

    def __init__(self, hass: HomeAssistant, driver: ZendureDriver | None, subentry_id: str | None, name: str, data: dict[str, Any], model: str) -> None:
        ZendureDevice.__init__(self, hass, driver, subentry_id, name, data, model)
        EntityDevice.__init__(self, hass, data["sn"], name, subentry_id)
        self.reader_sn: str = data.get("reader_sn", "")  # the reader announces itself on mDNS under its own serial
        self.connection = ConnectionType.OFFLINE
        self.mqtt: mqtt.Client | None = None

        # entity values: the declaration is the initial state, the entity writes it from here on
        self.totalPower = 0.0
        for phase in (1, 2, 3):
            setattr(self, f"phase{phase}_power", 0.0)
            setattr(self, f"phase{phase}_current", 0.0)
            setattr(self, f"phase{phase}_voltage", 0.0)

    def subentry_data(self) -> dict[str, Any]:
        return {**ZendureDevice.subentry_data(self), "reader_sn": self.reader_sn}

    def entity_create(self) -> None:
        HaSensor(self, "totalPower", uom="W", device_class="power", state_class="measurement")
        HaSensor(self, "totalImport", "totalActiveEnergy", uom="kWh", device_class="energy", state_class="total_increasing", convert=lambda v: v / 1000.0)
        HaSensor(self, "totalExport", "totalReverseEnergy", uom="kWh", device_class="energy", state_class="total_increasing", convert=lambda v: v / 1000.0)
        HaSensor(self, "rssi", uom="dBm", device_class="signal_strength", state_class="measurement")
        for phase in (1, 2, 3):
            HaSensor(self, f"phase{phase}_power", uom="W", device_class="power", state_class="measurement")
            HaSensor(self, f"phase{phase}_current", uom="A", device_class="current", state_class="measurement", convert=centi_convert)
            HaSensor(self, f"phase{phase}_voltage", uom="V", device_class="voltage", state_class="measurement", convert=centi_convert)

    def update_properties(self, payload: dict, now: float, on_ha: bool = False) -> None:
        """Handle incoming MQTT telemetry: flat properties and the per-phase circuits array; it creates no entities, so the thread does not matter."""
        # Flat properties: {totalPower: watts, ...}
        if properties := payload.get("properties"):
            for key, value in properties.items():
                self.entity_update(key, value)

        # Per-phase circuits: [{phase: 1, p: watts, i: amps*100, u: volts*100}, ...]
        if circuits := payload.get("circuits"):
            powers: list[int] = []
            for phase_data in circuits:
                phase_num = phase_data.get("phase")
                if phase_num is None:
                    continue
                if (p := phase_data.get("p")) is not None:
                    powers.append(p)
                for field, name in (("p", "power"), ("i", "current"), ("u", "voltage")):
                    if (v := phase_data.get(field)) is not None:
                        self.entity_update(f"phase{phase_num}_{name}", v)

            # a 3CT reports no totalPower, only its phases: then the total is their sum
            if powers and "totalPower" not in (properties or {}):
                self.entity_update("totalPower", sum(powers))
