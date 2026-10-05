"""Base entity and device classes for the Zendure integration."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import unicodedata
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def snakecase(value: str) -> str:
    """Convert to snake_case with only HA-valid chars (a-z, 0-9, _)."""
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").replace("+", "plus")
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"[^a-z0-9]", "_", value.lower())
    return re.sub(r"_+", "_", value).strip("_")


def device_by_identifier(hass: HomeAssistant, identifier: str) -> dr.DeviceEntry | None:
    # entry-scoped lookup where the core has it (HA >= 2026.8 deprecates async_get_device), else the registry-wide one
    registry = dr.async_get(hass)
    if (entries := hass.config_entries.async_entries(DOMAIN)) and hasattr(registry, "async_get_device_by_identifier"):
        return registry.async_get_device_by_identifier((DOMAIN, identifier), entries[0].entry_id)
    return registry.async_get_device(identifiers={(DOMAIN, identifier)})


def unique_name(family: str, sn: str, taken: set[str]) -> str:
    # the shortest serial tail (from 2 characters) that is free in the fleet; a newcomer never renames an existing device
    return next((f"{family}_{sn[-i:]}" for i in range(2, len(sn) + 1) if f"{family}_{sn[-i:]}" not in taken), f"{family}_{sn}")


class HaEntity(Entity):
    """Base for all entities."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, device: EntityDevice, key: str, vendor_id: str | None, disabled: bool = False) -> None:
        self._attr_entity_registry_enabled_default = not disabled  # must be set before the platform adds us, or HA registers us enabled
        self.vendor_id = vendor_id or key
        device.entities[key] = self  # reachable by display key and by vendor id
        device.entities[self.vendor_id] = self
        self.attr = key  # resolved lazily in set_attr: the device fields are created after entity_create()
        self.device = device
        # both ids follow the device name, which is computed once at creation and never changes afterwards
        self._attr_unique_id = snakecase(f"{device.name}_{key}")
        self.internal_integration_suggested_object_id = self._attr_unique_id
        self._attr_translation_key = snakecase(key)

    @property
    def device_info(self) -> DeviceInfo | None:
        return self.device.device_info

    @property
    def state(self) -> Any:
        return None

    def update_value(self, value: Any, restore: bool = False) -> bool:
        """Update entity state. Called when new data arrives."""
        try:
            if not self.set_state(value, restore):
                return False
            if self.hass and self.hass.loop.is_running():
                self.schedule_update_ha_state()
            return True
        except Exception as err:
            _LOGGER.error("Error updating %s: %s", self._attr_unique_id, err)
            return False

    def set_attr(self, value: Any) -> None:
        if self.attr and hasattr(self.device, self.attr):
            setattr(self.device, self.attr, value)

    def set_state(self, value: Any, restore: bool = False) -> bool:
        """Convert, compare, assign. Return True if changed. Override per platform."""
        return False

    async def _invoke(self, callback: Callable | None, *args: Any) -> None:
        """Invoke sync or async callback."""
        if callback is not None:
            if inspect.iscoroutinefunction(callback):
                await callback(*args)
            else:
                callback(*args)


class EntityDevice:
    """Base device that manages entities and state persistence."""

    SENSORS: dict[str, tuple[type[HaEntity], str, dict[str, Any]]] = {}

    def __init__(self, hass: HomeAssistant, device_id: str, name: str | None = None, subentry_id: str | None = None) -> None:
        """Build on an existing HA registry entry; everything descriptive lives there, not here."""
        self.hass = hass
        self.device_id = device_id
        self.name = name or device_id  # the stable name every unique_id and entity_id is built on
        self.subentry_id = subentry_id  # set for a device that belongs to a config subentry: its entities are filed there too
        self.device_info = DeviceInfo(identifiers={(DOMAIN, device_id)})
        self.entities: dict[str, HaEntity | None] = {}
        self._restore: list[HaEntity] = []
        self.entity_create()
        if (device := device_by_identifier(self.hass, self.device_id)) is not None:
            names = {snakecase(f"{self.name}_{name}"): key for key, (_, name, _) in self.SENSORS.items()}
            for entry in er.async_entries_for_device(er.async_get(self.hass), device.id, include_disabled_entities=True):
                if (key := names.get(entry.unique_id)) and key not in self.entities:
                    cls, name, kwargs = self.SENSORS[key]
                    cls(self, name, vendor_id=key, **kwargs)

    def unload(self) -> None:
        """Override to unload if necessary."""

    def subentry_data(self) -> dict[str, Any]:
        """The fields a subentry-backed device keeps in its subentry; the coordinator persists them when they change."""
        return {}

    def add_entity(self, entity: HaEntity) -> None:
        # hand a new entity to its HA platform; on a subentry-backed device it is filed under that subentry
        if self.subentry_id:
            entity.add([entity], config_subentry_id=self.subentry_id)
        else:
            entity.add([entity])

    def entity_create(self) -> None:
        """Override to create the entities: HaXxx(self, "<telemetry id>", "<value attribute>", ...)."""

    def device_load(self, store: dict[str, Any]) -> None:
        """Apply the persisted section; the payload is already in memory, so this is sync."""
        for entity in self._restore:
            if (value := store.get(entity._attr_unique_id)) is not None:
                entity.update_value(value, restore=True)

    def device_save(self, store: dict[str, Any]) -> None:
        """Persist restorable entity states to Store; subclasses pass their own attributes via extra."""
        for entity in self._restore:
            if (s := entity.state) is not None:
                store[entity._attr_unique_id] = s

    def entity_update(self, key: str, value: Any, on_ha: bool = False) -> bool | None:
        """Route incoming data to the correct entity; None when there is no entity for the key."""
        if entity := self.entities.get(key):
            return entity.update_value(value)

        if key not in self.SENSORS:
            self.entities[key] = None
            return False

        # Sensors contains all information to create the correct HaEntity
        cls, name, kwargs = self.SENSORS[key]

        # a property the vendor spells more than one way shares the entity the first spelling created
        if (entity := self.entities.get(name)) is not None:
            self.entities[key] = entity
            return entity.update_value(value)

        entity = cls(self, name, vendor_id=key, **kwargs) if on_ha else asyncio.run_coroutine_threadsafe(self._entity_add(cls, name, key, kwargs), self.hass.loop).result()
        return entity.update_value(value) if entity else False

    async def _entity_add(self, cls, name, key, kwargs) -> HaEntity:
        return cls(self, name, vendor_id=key, **kwargs)

    def entity_write(self, entity: HaEntity, value: Any) -> None:
        """Write value to hardware. Override in device subclasses."""
