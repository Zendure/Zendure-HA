"""Interfaces with the Zendure Integration api sensors."""

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.template import Template
from homeassistant.util import dt as dt_util
from stringcase import snakecase

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(_hass: HomeAssistant, _config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the Zendure sensor."""
    ZendureSensor.addSensors = async_add_entities


class ZendureSensor(SensorEntity):
    addSensors: AddEntitiesCallback

    def __init__(
        self,
        deviceinfo: DeviceInfo,
        uniqueid: str,
        template: Template | None = None,
        uom: str | None = None,
        deviceclass: Any | None = None,
        stateclass: Any | None = None,
        precision: int | None = None,
    ) -> None:
        """Initialize a Zendure entity."""
        self._attr_has_entity_name = True
        self._attr_should_poll = False
        self._attr_available = True
        self.entity_description = SensorEntityDescription(
            key=uniqueid, name=uniqueid, native_unit_of_measurement=uom, device_class=deviceclass, state_class=stateclass
        )
        self._attr_device_info = deviceinfo
        self._attr_unique_id = f"{deviceinfo.get('name', None)}-{uniqueid}"
        self.entity_id = f"sensor.{deviceinfo.get('name', None)}-{snakecase(uniqueid)}"
        self._attr_translation_key = snakecase(uniqueid)
        self._value_template: Template | None = template
        if precision is not None:
            self._attr_suggested_display_precision = precision

    def update_value(self, value: Any) -> None:
        try:
            new_value = self._value_template.async_render_with_possible_json_value(value, None) if self._value_template is not None else value

            if self.hass and new_value != self._attr_native_value:
                self._attr_native_value = new_value
                if self.hass and self.hass.loop.is_running():
                    self.schedule_update_ha_state()

        except Exception as err:
            self._attr_native_value = value
            _LOGGER.error(f"Error {err} setting state: {self._attr_unique_id} => {value}")


class ZendureRestoreSensor(ZendureSensor, RestoreEntity):
    """Representation of a Zendure select entity with restore."""

    def __init__(
        self,
        deviceinfo: DeviceInfo,
        uniqueid: str,
        template: Template | None = None,
        uom: str | None = None,
        deviceclass: Any | None = None,
        stateclass: Any | None = None,
        precision: int | None = None,
    ) -> None:
        """Initialize a select entity."""
        super().__init__(deviceinfo, uniqueid, template, uom, deviceclass, stateclass, precision)
        self.last_value = 0
        self.lastValueUpdate = dt_util.utcnow()

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        if state is not None and state.state != "unknown":
            self._attr_native_value = state.state
            self._attr_last_reset = dt_util.utcnow()
            _LOGGER.debug(f"Restored state for {self.entity_id}: {self._attr_native_value}")

    def aggregate(self, time: datetime, value: int, allowReset: bool) -> None:
        # reset the aggregate sensors each day
        if self.state is None or self.last_reset is None or (self.last_reset.date() != time.date() and allowReset):
            self._attr_native_value = 0.0
            self._attr_last_reset = time
        else:
            secs = time.timestamp() - self.lastValueUpdate.timestamp()
            self._attr_native_value = float(self.state) + self.last_value * secs / 3600000

        _LOGGER.debug(f"Saved state for {self.entity_id}: {self._attr_native_value}")
        self.last_value = value
        self.lastValueUpdate = time
        if self.hass and self.hass.loop.is_running():
            self.schedule_update_ha_state()
