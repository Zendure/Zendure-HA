"""Button platform for the Zendure integration."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import EntityDevice, HaEntity


async def async_setup_entry(
    _hass: HomeAssistant,
    _config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the buttons."""
    HaButton.add = async_add_entities


class HaButton(ButtonEntity, HaEntity):
    # a row action as an HA button: pressing it runs the callback, sync or async

    add: AddEntitiesCallback

    def __init__(self, device: EntityDevice, key: str, vendor_id: str | None = None, press: Callable | None = None, disabled: bool = False) -> None:
        super().__init__(device, key, vendor_id, disabled)
        self._press = press
        self.entity_description = ButtonEntityDescription(key=key, name=key)
        device.add_entity(self)

    async def async_press(self) -> None:
        await self._invoke(self._press, self)
