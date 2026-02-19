# """Initialize the Zendure component."""

import logging

from homeassistant.components import mqtt
from homeassistant.components.recorder import get_instance
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from stringcase import snakecase

from custom_components.zendure_ha.api import Api

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
    Api.cloud.loop_stop()
    Api.local.loop_stop()
    result = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if result:
        await entry.runtime_data.async_unload()
    return result


async def async_remove_config_entry_device(_hass: HomeAssistant, _entry: ZendureConfigEntry, device_entry: dr.DeviceEntry) -> bool:
    """Remove a device from a config entry."""
    for d in Api.devices.values():
        if d.name == device_entry.name:
            Api.devices.pop(d.deviceId)
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

    if entry.version == 1 and entry.minor_version <= 3:  # noqa: PLR2004
        # Rename the device ids
        device_registry = dr.async_get(hass)
        entity_registry = er.async_get(hass)
        delete: list[str] = [
            "ac_status",
            "ai_state",
            "bindstate",
            "blue_ota",
            "circuit_check_mode",
            "coupling_state",
            "ct_off",
            "data_ready",
            "dc_status",
            "dry_node_state",
            "dspversion",
            "energy_power",
            "factory_mode_state",
            "fault_level",
            "grid_input_power",
            "grid_reverse",
            "grid_standard",
            "hub_state",
            "i_o_t_state",
            "input_mode",
            "is_error",
            "l_c_n_state",
            "lamp_switch",
            "local_a_p_i_enable",
            "local_state",
            "master_soft_version",
            "master_switch",
            "masterhaer_version",
            "old_mode",
            "output_home_power",
            "output_pack_power",
            "pack_input_power",
            "pack_num",
            "phase_switch",
            "pv_brand",
            "reverse_state",
            "rssi",
            "smart_mode",
            "strength",
            "volt_wakeup",
            "wifi_state",
            "write_rsp",
        ]
        rename = {"solar_input_power": "solar_power", "min_soc": "soc_min", "max_soc": "soc_max", "hyper_temp": "temp"}
        devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        for device in devices:
            if device.name == "Zendure Manager":
                device_name = "Zendure Manager"
                device_registry.async_update_device(device.id, name=device_name)
            elif device.model_id is not None and (m := Api.models.get(device.model_id.lower())) is not None:
                # Rename the device to match the model and serial number
                device_name = f"{m[0].replace(' ', '').replace('SolarFlow', 'SF')} {device.serial_number[-2:] if device.serial_number is not None else ''}".strip()
                if device_name != device.name:
                    _LOGGER.debug("Renaming device %s to %s", device.name, device_name)
                    device_registry.async_update_device(device.id, name=device_name, name_by_user=device.name)

                def update_entity(entity: er.RegistryEntry, unique_id: str, device_name_new: str) -> None:
                    # Update the entity name
                    try:
                        uniqueid = snakecase(f"{device_name_new.lower()}_{unique_id}").replace("__", "_")
                        entityid = f"{entity.domain}.{uniqueid}"
                        if entity.entity_id != entityid or entity.unique_id != uniqueid or entity.translation_key != unique_id:
                            entity_registry.async_remove(entityid)
                            get_instance(hass).async_clear_statistics([entityid])
                            entity_registry.async_update_entity(entity.entity_id, new_unique_id=uniqueid, new_entity_id=entityid, translation_key=unique_id)
                            _LOGGER.debug("Updated entity %s unique_id to %s", entity.entity_id, uniqueid)
                    except Exception as e:
                        entity_registry.async_remove(entity.entity_id)
                        _LOGGER.error("Failed to update entity %s: %s", entity.entity_id, e)

                # Update the device entities
                entities = er.async_entries_for_device(entity_registry, device.id, True)
                device_name = snakecase(device_name.lower())

                for entity in entities:
                    unique_id = entity.translation_key or ""
                    if unique_id in delete:
                        entity_registry.async_remove(entity.entity_id)
                    elif (new_unique_id := rename.get(unique_id)) is not None:
                        update_entity(entity, new_unique_id, device_name)
                    elif unique_id.startswith("aggr"):
                        update_entity(entity, unique_id.replace("_total", ""), device_name)
                    else:
                        update_entity(entity, unique_id, device_name)

        hass.config_entries.async_update_entry(entry, version=1, minor_version=3)

    _LOGGER.debug("Migration to version %s:%s successful", entry.version, entry.minor_version)

    return True
