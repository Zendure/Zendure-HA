"""Interfaces with the Zendure Integration switch."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.template import Template

from .entity import EntityDevice, EntityZendure

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(_hass: HomeAssistant, _config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the Zendure switch."""
    ZendureSwitch.register_adder(async_add_entities)


class ZendureSwitch(EntityZendure, SwitchEntity):
    add: AddEntitiesCallback

    def __init__(
        self,
        device: EntityDevice,
        uniqueid: str,
        onwrite: Callable,
        template: Template | None = None,
        deviceclass: Any | None = None,
        value: bool | None = None,
    ) -> None:
        """Initialize a switch entity."""
        super().__init__(device, uniqueid)
        self.entity_description = SwitchEntityDescription(key=uniqueid, name=uniqueid, device_class=deviceclass)

        self._attr_available = True
        self._value_template: Template | None = template
        self._onwrite = onwrite
        if value is not None:
            self._attr_is_on = value
        self.add_to_platform()

    def update_value(self, value: Any) -> bool:
        try:
            is_on = bool(int(self._value_template.async_render_with_possible_json_value(value, None)) != 0 if self._value_template is not None else int(value) != 0)

            if self._attr_is_on == is_on:
                return False

            _LOGGER.info(f"Update switch: {self._attr_unique_id} => {is_on}")

            self._attr_is_on = is_on
            if self.hass and self.hass.loop.is_running():
                self.schedule_update_ha_state()
        except Exception as err:
            _LOGGER.error(f"Error {err} setting state: {self._attr_unique_id} => {value}")
        return True

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn switch on."""
        if asyncio.iscoroutinefunction(self._onwrite):
            await self._onwrite(self, 1)
        else:
            self._onwrite(self, 1)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn switch off."""
        if asyncio.iscoroutinefunction(self._onwrite):
            await self._onwrite(self, 0)
        else:
            self._onwrite(self, 0)
