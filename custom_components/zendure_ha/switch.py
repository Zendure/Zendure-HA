"""Switch platform for the Zendure integration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import EntityDevice, HaEntity


async def async_setup_entry(
    _hass: HomeAssistant,
    _config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the switches."""
    HaSwitch.add = async_add_entities


class HaSwitch(SwitchEntity, HaEntity):
    """Switch entity."""

    add: AddEntitiesCallback

    def __init__(self, device: EntityDevice, key: str, vendor_id: str | None = None, on_changed: Callable | None = None, restore: bool = False, default: bool = False, disabled: bool = False) -> None:

        super().__init__(device, key, vendor_id, disabled)
        self._on_changed = on_changed
        self.entity_description = SwitchEntityDescription(key=key, name=key)
        if restore:
            device._restore.append(self)
        device.add_entity(self)

    def set_state(self, value: Any, restore: bool = False) -> bool:
        value = value if isinstance(value, bool) else str(value).lower() in ("on", "true", "1")
        if self._attr_is_on == value:
            return False
        self._attr_is_on = value
        self.set_attr(value)
        return True

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._apply(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._apply(False)

    async def _apply(self, on: bool) -> None:
        self._attr_is_on = on
        self.set_attr(on)  # the device attribute follows, as the select does; the callback then sees the new value in place
        if self._on_changed is not None:
            await self._invoke(self._on_changed, self, on)
        else:
            await self._invoke(self.device.entity_write, self, int(on))  # a plain device property: 0/1, the way the telemetry reports it
        self.async_write_ha_state()
