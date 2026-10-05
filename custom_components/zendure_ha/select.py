"""Select platform for the Zendure integration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import EntityDevice, HaEntity


async def async_setup_entry(
    _hass: HomeAssistant,
    _config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the selects."""
    HaSelect.add = async_add_entities


class HaSelect(SelectEntity, HaEntity):
    """Select entity."""

    add: AddEntitiesCallback

    def __init__(
        self,
        device: EntityDevice,
        key: str,
        options: dict[Any, str],
        vendor_id: str | None = None,
        on_changed: Callable | None = None,
        restore: bool = False,
        disabled: bool = False,
    ) -> None:
        super().__init__(device, key, vendor_id, disabled)
        self._options = options
        self._attr_options = list(options.values())
        self._on_changed = on_changed
        self.entity_description = SelectEntityDescription(key=key, name=key)
        if restore:
            device._restore.append(self)
        device.add_entity(self)

    def _key(self, option: Any, default: Any = None) -> Any:
        # the option string is only the display label; the key is the semantic value
        return next((k for k, v in self._options.items() if v == option), default)

    def set_state(self, value: Any, restore: bool = False) -> bool:
        key = self._key(value, value)  # accept the option name as well as the key
        option = self._options.get(key)
        if option is None or self._attr_current_option == option:
            return False
        self._attr_current_option = option
        self.set_attr(key)  # the key is the semantic value; the option string is only the display label
        return True

    async def async_select_option(self, option: str) -> None:
        """Handle option selected from UI."""
        self._attr_current_option = option
        key = self._key(option, option)
        self.set_attr(key)  # the key is the semantic value; the option string is only the display label
        if self._on_changed is not None:
            await self._invoke(self._on_changed, self, self._attr_current_option)
        else:
            await self._invoke(self.device.entity_write, self, key)  # a plain device property: the key goes to the hardware
        self.async_write_ha_state()

    @property
    def value(self) -> Any:
        return self._key(self._attr_current_option)
