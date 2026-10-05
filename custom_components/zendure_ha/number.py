"""Number platform for the Zendure integration."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import EntityDevice, HaEntity


async def async_setup_entry(
    _hass: HomeAssistant,
    _config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the numbers."""
    HaNumber.add = async_add_entities


class HaNumber(NumberEntity, HaEntity):
    """Number entity."""

    add: AddEntitiesCallback

    def __init__(
        self,
        device: EntityDevice,
        key: str,
        vendor_id: str | None = None,
        *,
        uom: str | None = None,
        factor: float = 1.0,
        minimum: float = 0,
        maximum: float = 2000,
        mode: NumberMode = NumberMode.AUTO,
        step: float | None = None,
        restore: bool = False,
        disabled: bool = False,
    ) -> None:
        super().__init__(device, key, vendor_id, disabled)
        self.factor = factor
        self._step = step  # NumberEntity owns `step`; a float knob keeps the resolution of its step; None is the integer device knob
        self._decimals = max(0, -Decimal(str(step)).normalize().as_tuple().exponent) if step is not None else 0
        self.entity_description = NumberEntityDescription(
            key=key,
            name=key,
            native_unit_of_measurement=uom,
        )
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_mode = mode
        if step is not None:
            self._attr_native_step = step
        if restore:
            device._restore.append(self)
        device.add_entity(self)

    def _round(self, raw: float) -> float:
        # an integer knob truncates, a stepped knob keeps the resolution of its step
        return int(raw) if self._step is None else int(round(raw)) if not self._decimals else round(raw, self._decimals)

    def set_state(self, value: Any, restore: bool = False) -> bool:
        value = self._round(float(value) * self.factor if not restore else float(value))
        if self._attr_native_value == value:
            return False
        self._attr_native_value = value
        self.set_attr(value)
        return True

    async def async_set_native_value(self, value: float) -> None:
        # optimistic update: soc knobs are not echoed back by the device, without this the UI never reflects the change
        raw = self._round(value / self.factor)
        await self._invoke(self.device.entity_write, self, raw)
        self.update_value(raw)

    def update_range(self, minimum: float, maximum: float) -> None:
        """Update min/max range."""
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        if self.hass and self.hass.loop.is_running():
            self.schedule_update_ha_state()
