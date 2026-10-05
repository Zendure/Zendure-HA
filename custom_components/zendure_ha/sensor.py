"""Sensor platform for the Zendure integration."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import EntityDevice, HaEntity

AGGREGATE_MAX_GAP_S = 300.0  # longer telemetry gaps are not integrated: the device state was unknown
AGGREGATE_PUSH_S = 60.0  # the counter integrates every telemetry tick but pushes to HA at most this often


async def async_setup_entry(
    _hass: HomeAssistant,
    _config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensors."""
    HaSensor.add = async_add_entities


class HaSensor(SensorEntity, HaEntity):
    """Sensor entity."""

    add: AddEntitiesCallback

    def __init__(
        self,
        device: EntityDevice,
        key: str,
        vendor_id: str | None = None,
        *,
        uom: str | None = None,
        device_class: str | None = None,
        state_class: str | None = None,
        precision: int | None = None,
        icon: str | None = None,
        aggr: bool = False,
        convert: Callable[[Any], Any] | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(device, key, vendor_id, disabled)
        self._attr_suggested_display_precision = precision
        self._convert = convert
        self._aggr: HaSensor | None = None
        self.entity_description = SensorEntityDescription(key=key, name=key, native_unit_of_measurement=uom, device_class=device_class, state_class=state_class, icon=icon)
        if aggr:
            # the kWh integrator lives beside its power sensor, under its own key
            self._aggr = HaSensor(device, f"{key}_aggr", uom="kWh", device_class="energy", state_class="total_increasing")
            self._aggr._aggregate_init()
            device._restore.append(self._aggr)
        device.add_entity(self)

    @property
    def state(self) -> Any:
        return self._attr_native_value

    def set_state(self, value: Any, restore: bool = False) -> bool:
        if self._convert is not None:
            value = self._convert(value)
        elif self._attr_suggested_display_precision:
            value = round(value, self._attr_suggested_display_precision)

        # integrate on every report, also unchanged ones: otherwise hours of constant power are lost on a restart
        if self._aggr is not None:
            self._aggr._aggregate(value)

        if self._attr_native_value == value:
            return False
        self._attr_native_value = value
        self.set_attr(value)
        return True

    def _aggregate_init(self) -> None:
        self._last_value = 0.0
        self._last_update = 0.0
        self._last_push = 0.0

    def _aggregate(self, aggr: float) -> None:
        """Accumulate kWh from watts."""
        # monotonic clock: NTP jumps and suspends must not distort the integration
        now = time.monotonic()

        # integrate the previous power over the elapsed interval; after a long gap the device state was unknown, so only re-prime
        if 0 < self._last_update and now - self._last_update <= AGGREGATE_MAX_GAP_S:
            dt_h = (now - self._last_update) / 3600.0
            kwh = (self._attr_native_value or 0.0) + self._last_value * dt_h / 1000.0
            self._attr_native_value = kwh

            # the counter updates every telemetry tick; throttle the HA push to spare the recorder
            if now - self._last_push >= AGGREGATE_PUSH_S and self.hass and self.hass.loop.is_running():
                self._last_push = now
                self.schedule_update_ha_state()
        self._last_value = aggr
        self._last_update = now
