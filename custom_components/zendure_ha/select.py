"""Interfaces with the Zendure Integration."""

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from stringcase import snakecase

from .device import Device

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(_hass: HomeAssistant, _config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the Zendure select."""
    ZendureSelect.add = async_add_entities


class ZendureSelect(SelectEntity):
    """Representation of a Zendure select entity."""

    add: AddEntitiesCallback

    def __init__(self, device: Device, uniqueid: str, options: dict[Any, str], onchanged: Callable | None, current: int | None = None) -> None:
        """Initialize a select entity."""
        self._attr_has_entity_name = True
        self._attr_should_poll = False
        self.entity_description = SelectEntityDescription(key=uniqueid, name=uniqueid)
        self._attr_device_info = device.attr_device_info
        self.legacy_mode = device.definition.get("isLegacy", False)
        self._attr_unique_id = f"{self._attr_device_info.get('name', None)}-{uniqueid}"
        self.entity_id = f"select.{self._attr_device_info.get('name', None)}-{snakecase(uniqueid)}"
        self._attr_translation_key = snakecase(uniqueid)

        self._options = options
        self._attr_options = list(options.values())
        if current:
            self._attr_current_option = options[current]
        else:
            self._attr_current_option = self._attr_options[0]
        self.onchanged = onchanged
        device.entities[uniqueid] = self
        # Ensure add is called on the main thread/event loop
        if self.hass and self.hass.loop.is_running():
            self.hass.loop.call_soon_threadsafe(self.add, [self])
        else:
            device.call_threadsafe(self.add, [self])

    def setOptions(self, options: dict[Any, str]) -> None:
        """Set the options for the select entity."""
        self._options = options
        self._attr_options = list(options.values())
        if self._attr_current_option not in self._attr_options:
            self._attr_current_option = self._attr_options[0]
        self.async_write_ha_state()

    def update_value(self, value: Any) -> None:
        try:
            if self.translation_key == "cluster":
                _LOGGER.debug(f"Update cluster state: {self._attr_unique_id} => {value}")

            if value not in self._options:
                return
            new_value = self._options[value]
            if new_value != self._attr_current_option:
                self._attr_current_option = new_value
                if self.hass and self.hass.loop.is_running():
                    _LOGGER.info(f"Update sensor state: {self._attr_unique_id} => {new_value}")
                    self.schedule_update_ha_state()

        except Exception as err:
            _LOGGER.error(f"Error {err} setting state: {self._attr_unique_id} => {value}")

    async def async_select_option(self, option: str) -> None:
        # """Update the current selected option."""
        if self.legacy_mode:
            for key, value in self._options.items():
                if value == option:
                    self._attr_current_option = option
                    self.async_write_ha_state()
                    if self.onchanged:
                        self.onchanged(self, key)
                    break
        else:
            self._attr_current_option = option
            self.async_write_ha_state()
            if self.onchanged:
                self.onchanged(self, option)

    @property
    def value(self) -> Any:
        for key, value in self._options.items():
            if value == self.current_option:
                return key
        return None


class ZendureRestoreSelect(ZendureSelect, RestoreEntity):
    """Representation of a Zendure select entity with restore."""

    def __init__(self, device: Device, uniqueid: str, options: dict[int, str], onchanged: Callable | None, current: int | None = None) -> None:
        """Initialize a select entity."""
        super().__init__(device, uniqueid, options, onchanged, current)

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        if state := await self.async_get_last_state():
            self.current_option = state.state
        else:
            self.current_option = self._attr_options[0]

        # do the onchanged callback
        if self.onchanged:
            self.onchanged(self, self.current_option)
