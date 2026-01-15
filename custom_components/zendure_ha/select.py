"""Interfaces with the Zendure Integration."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import ZendureEntities, ZendureEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(_hass: HomeAssistant, _config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the Zendure select."""
    ZendureSelect.add = async_add_entities


class ZendureSelect(ZendureEntity, SelectEntity):
    """Representation of a Zendure select entity."""

    add: AddEntitiesCallback

    def __init__(self, device: ZendureEntities, uniqueid: str, options: dict[Any, str], onchanged: Callable | None, current: int | None = None) -> None:
        """Initialize a select entity."""
        super().__init__(device, uniqueid, "select")
        self.entity_description = SelectEntityDescription(key=uniqueid, name=uniqueid)
        self._options = options
        self._attr_options = list(options.values())
        if current:
            self._attr_current_option = options[current]
        else:
            self._attr_current_option = self._attr_options[0]
        self.onchanged = onchanged
        self.add([self])

    def setDict(self, options: dict[Any, str]) -> None:
        """Set the options for the select entity."""
        self._options = options
        self._attr_options = list(options.values())
        if self._attr_current_option not in self._attr_options:
            self._attr_current_option = self._attr_options[0]
        if self.hass and self.hass.loop.is_running():
            self.async_write_ha_state()

    def setList(self, options: list[str]) -> None:
        """Set the options for the select entity."""
        self._options = None
        self._attr_options = options
        if self._attr_current_option not in self._attr_options:
            self._attr_current_option = self._attr_options[0]
        if self.hass and self.hass.loop.is_running():
            self.async_write_ha_state()

    def update_value(self, value: Any) -> bool:
        try:
            if value not in self._options:
                return False

            if self._options is not None:
                new_value = self._options[value]
                if new_value != self._attr_current_option:
                    self._attr_current_option = new_value
                    if self.hass and self.hass.loop.is_running():
                        self.schedule_update_ha_state()

        except Exception as err:
            _LOGGER.error(f"Error {err} setting state: {self._attr_unique_id} => {value}")
        return True

    async def async_select_option(self, option: str) -> None:
        """Update the current selected option."""
        self._attr_current_option = option
        value = self.value
        if self.onchanged:
            if asyncio.iscoroutinefunction(self.onchanged):
                await self.onchanged(self, value)
            else:
                self.onchanged(self, value)
        self.async_write_ha_state()

    @property
    def value(self) -> Any:
        if self._options is not None:
            for key, value in self._options.items():
                if value == self._attr_current_option:
                    return key
        return None


class ZendureRestoreSelect(ZendureSelect, RestoreEntity):
    """Representation of a Zendure select entity with restore."""

    def __init__(self, device: ZendureEntities, uniqueid: str, options: dict[int, str], onchanged: Callable | None, current: int | None = None) -> None:
        """Initialize a select entity."""
        super().__init__(device, uniqueid, options, onchanged, current)

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        if state := await self.async_get_last_state():
            self._attr_current_option = state.state
        else:
            self._attr_current_option = self._attr_options[0]

        # do the onchanged callback
        if self.onchanged:
            if asyncio.iscoroutinefunction(self.onchanged):
                await self.onchanged(self, self.current_option)
            else:
                self.onchanged(self, self.current_option)
