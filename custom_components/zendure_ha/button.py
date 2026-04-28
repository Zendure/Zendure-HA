"""Button platform for Zendure integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import EntityDevice, EntityZendure


async def async_setup_entry(_hass: HomeAssistant, _config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the Zendure button."""
    ZendureButton.add = async_add_entities


class ZendureButton(EntityZendure, ButtonEntity):
    add: AddEntitiesCallback

    def __init__(self, device: EntityDevice, uniqueid: str, onpress: Callable) -> None:
        """Initialize a button."""
        super().__init__(device, uniqueid, "button")
        self.entity_description = ButtonEntityDescription(key=uniqueid, name=uniqueid)
        self._onpress = onpress
        self.add([self])

    async def async_press(self) -> None:
        """Press the button."""
        if asyncio.iscoroutinefunction(self._onpress):
            await self._onpress(self)
        else:
            self._onpress(self)
