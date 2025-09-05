import logging
import asyncio

"""Interfaces with the Zendure Integration number."""

from collections.abc import Callable
from typing import Any

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.template import Template

from .entity import EntityDevice, EntityZendure

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(_hass: HomeAssistant, _config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the Zendure number."""
    ZendureNumber.add = async_add_entities


class ZendureNumber(EntityZendure, NumberEntity):
    add: AddEntitiesCallback

    def __init__(
        self,
        device: EntityDevice,
        uniqueid: str,
        onwrite: Callable | None,
        template: Template | None = None,
        uom: str | None = None,
        deviceclass: Any | None = None,
        maximum: int = 2000,
        minimum: int = 0,
        mode: NumberMode = NumberMode.AUTO,
        factor: int = 1,
        doupdate: bool = False,
    ) -> None:
        """Initialize a number entity."""
        super().__init__(device, uniqueid, "number")
        self.entity_description = NumberEntityDescription(
            key=uniqueid,
            name=uniqueid,
            native_unit_of_measurement=uom,
            device_class=deviceclass,
        )

        self._value_template: Template | None = template
        self._onwrite = onwrite
        self._attr_native_max_value = maximum
        self._attr_native_min_value = minimum
        self._attr_mode = mode
        self.factor = factor
        self.doupdate = doupdate
        device.add_entity(self.add, self)

    def update_value(self, value: Any) -> bool:
        try:
            new_value = (
                int(float(self._value_template.async_render_with_possible_json_value(value, None)) if self._value_template is not None else float(value))
                / self.factor
            )

            if self._attr_native_value == new_value:
                return False

            _LOGGER.info(f"Update number: {self._attr_unique_id} => {new_value}")

            self._attr_native_value = new_value
            if self.hass and self.hass.loop.is_running():
                self.schedule_update_ha_state()
        except Exception as err:
            _LOGGER.error(f"Error {err} setting state: {self._attr_unique_id} => {value}")

        return True

    async def async_set_native_value(self, value: float) -> None:
        """Set the value."""
        if self.doupdate:
            self._attr_native_value = value
            if self.hass and self.hass.loop.is_running():
                self.schedule_update_ha_state()

        if self._onwrite is not None:
            if asyncio.iscoroutinefunction(self._onwrite):
                await self._onwrite(self, int(self.factor * value))
            else:
                self._onwrite(self, int(self.factor * value))

    def update_range(self, minimum: int, maximum: int) -> None:
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        if self.hass and self.hass.loop.is_running():
            self.schedule_update_ha_state()

    @property
    def asNumber(self) -> int | float:
        """Return the current value of the sensor."""
        return self._attr_native_value if isinstance(self._attr_native_value, (int, float)) else 0


class ZendureRestoreNumber(ZendureNumber, RestoreEntity):
    """Representation of a Zendure number entity with restore."""

    def __init__(
        self,
        device: EntityDevice,
        uniqueid: str,
        onwrite: Callable | None,
        template: Template | None = None,
        uom: str | None = None,
        deviceclass: Any | None = None,
        maximum: int = 2000,
        minimum: int = 0,
        mode: NumberMode = NumberMode.AUTO,
        doupdate: bool = False,
    ) -> None:
        """Initialize a number entity."""
        super().__init__(device, uniqueid, onwrite, template, uom, deviceclass, maximum, minimum, mode, 1, doupdate)
        self._attr_native_value = 0

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        if state := await self.async_get_last_state():
            if state.state is None or state.state == "unknown":
                self._attr_native_value = 0
                return
            self._attr_native_value = int(float(state.state))
            if self._onwrite is not None:
                if asyncio.iscoroutinefunction(self._onwrite):
                    await self._onwrite(self, self._attr_native_value)
                else:
                    self._onwrite(self, self._attr_native_value)
