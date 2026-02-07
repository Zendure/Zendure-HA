# """Initialize the Zendure component."""

import logging

from homeassistant.components import mqtt
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from stringcase import snakecase

from .coordinator import ZendureConfigEntry, ZendureCoordinator
from .device import ZendureDevice

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.SELECT, Platform.SENSOR, Platform.SWITCH]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ZendureConfigEntry) -> bool:
    """Set up Zendure as config entry."""
    if not await mqtt.async_wait_for_mqtt_client(hass):
        _LOGGER.error("MQTT integration not available")
        raise ConfigEntryNotReady("MQTT integration not available")  # noqa: TRY003

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator = ZendureCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.async_config_entry_first_refresh()
    entry.async_on_unload(entry.add_update_listener(coordinator.async_update))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ZendureConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading Zendure config entry: %s", entry.entry_id)
    result = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if result:
        await entry.runtime_data.async_unload()
    return result


async def async_remove_config_entry_device(_hass: HomeAssistant, entry: ZendureConfigEntry, device_entry: dr.DeviceEntry) -> bool:
    """Remove a device from a config entry."""
    coordinator = entry.runtime_data

    # check for device to remove
    for d in coordinator.devices.values():
        if d.name == device_entry.name:
            coordinator.devices.pop(d.deviceId)
            return True

        if isinstance(d, ZendureDevice) and (bat := next((b for b in d.batteries.values() if b.name == device_entry.name), None)) is not None:
            d.batteries.pop(bat.deviceId)
            return True

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ZendureConfigEntry) -> bool:
    """Migrate entry."""
    _LOGGER.debug("Migrating from version %s:%s", entry.version, entry.minor_version)

    if entry.version > 1:
        # This means the user has downgraded from a future version
        return False

    if entry.version == 1 and entry.minor_version == 1:
        # Rename the device ids
        device_registry = dr.async_get(hass)
        entity_registry = er.async_get(hass)
        delete: list[str] = ["gridInputPower", "outputHomePower", "outputPackPower", "packInputPower"]
        rename = {"solarInputPower": "solarPower", "minSoc": "socMin"}
        devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        for device in devices:
            if device.model_id is not None and (m := ZendureCoordinator.models.get(device.model_id.lower())) is not None:
                device_name = f"{m[0].replace(' ', '').replace('SolarFlow', 'SF')} {device.serial_number[-2:] if device.serial_number is not None else ''}".strip()
                if device.name != device_name:

                    def update_entity(entity: er.RegistryEntry, unique_id: str, device_name: str) -> None:
                        # Update the entity name
                        try:
                            new_entity_id = f"{entity.domain}.{snakecase(device_name)}_{snakecase(unique_id)}"
                            new_unique_id = f"{device_name}-{unique_id}"
                            entity_registry.async_update_entity(
                                entity.entity_id,
                                new_entity_id=new_entity_id,
                                new_unique_id=new_unique_id,
                            )
                            _LOGGER.debug("Migrating entity %s", entity.entity_id)
                        except Exception as e:
                            _LOGGER.error("Error updating entity %s: %s", entity.entity_id, e)

                    # Update the device entities
                    entities = er.async_entries_for_device(entity_registry, device.id, True)
                    for entity in entities:
                        unique_id = entity.unique_id[entity.unique_id.find("-") + 1 :]
                        if unique_id in delete:
                            entity_registry.async_remove(entity.entity_id)
                        elif (new_unique_id := rename.get(unique_id)) is not None:
                            update_entity(entity, new_unique_id, device_name)
                        elif unique_id.startswith("aggr"):
                            update_entity(entity, unique_id.replace("Total", ""), device_name)
                        else:
                            update_entity(entity, unique_id, device_name)

                    _LOGGER.debug("Renaming device %s to %s", device.name, device_name)
                    device_registry.async_update_device(
                        device.id,
                        name=device_name,
                    )

    _LOGGER.debug("Migration to version %s:%s successful", entry.version, entry.minor_version)

    return True
