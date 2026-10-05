"""Time platform for the Zendure integration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import time
from typing import Any

from homeassistant.components.time import TimeEntity, TimeEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import EntityDevice, HaEntity


async def async_setup_entry(
    _hass: HomeAssistant,
    _config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the times."""
    HaTime.add = async_add_entities


class HaTime(TimeEntity, HaEntity):
    # a clock time kept on the device as "HH:MM" text; empty text is no time

    add: AddEntitiesCallback

    def __init__(self, device: EntityDevice, key: str, vendor_id: str | None = None, on_changed: Callable | None = None, restore: bool = False, disabled: bool = False) -> None:
        super().__init__(device, key, vendor_id, disabled)
        self._on_changed = on_changed
        self.entity_description = TimeEntityDescription(key=key, name=key)
        if restore:
            device._restore.append(self)
        device.add_entity(self)

    def set_state(self, value: Any, restore: bool = False) -> bool:
        # "HH:MM" from the device, "HH:MM:SS" from HA's last state
        text = "" if value is None or str(value) in ("unknown", "unavailable") else str(value)
        parsed = time(*(int(p) for p in text.split(":")[:2])) if text else None
        if self._attr_native_value == parsed:
            return False
        self._attr_native_value = parsed
        self.set_attr(parsed.strftime("%H:%M") if parsed else "")
        return True

    async def async_set_value(self, value: time) -> None:
        self.update_value(value.strftime("%H:%M"))
        await self._invoke(self._on_changed, self, value)
