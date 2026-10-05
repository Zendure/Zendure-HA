"""Binary sensor platform for the Zendure integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import EntityDevice, HaEntity


async def async_setup_entry(
    _hass: HomeAssistant,
    _config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary sensors."""
    HaBinarySensor.add = async_add_entities


class HaBinarySensor(BinarySensorEntity, HaEntity):
    """Binary sensor entity."""

    add: AddEntitiesCallback

    def __init__(
        self,
        device: EntityDevice,
        key: str,
        vendor_id: str | None = None,
        *,
        device_class: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(device, key, vendor_id, disabled)
        self.entity_description = BinarySensorEntityDescription(
            key=key,
            name=key,
            device_class=device_class,
        )
        device.add_entity(self)

    def set_state(self, value: Any, restore: bool = False) -> bool:
        # device payloads mix numbers, numeric strings and on/true words; anything else counts as off
        try:
            value = float(value) != 0
        except (TypeError, ValueError):
            value = str(value).strip().lower() in ("on", "true", "yes")
        if self._attr_is_on == value:
            return False
        self._attr_is_on = value
        self.set_attr(value)
        return True
