"""Initialize the Zendure component."""

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .api import Api
from .const import CONF_MQTTLOG, CONF_P1METER, CONF_SIM
from .device import ZendureDevice
from .manager import ZendureConfigEntry, ZendureManager
from .migration import Migration

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.SELECT, Platform.SENSOR, Platform.SWITCH]

_LOGGER = logging.getLogger(__name__)


async def async_migrate_entry(hass: HomeAssistant, entry: ZendureConfigEntry) -> bool:
    """Migrate config entry to new version."""
    if entry.version == 1 and entry.minor_version < 5:
        _LOGGER.info("Migrating Zendure config entry from version %s.%s", entry.version, entry.minor_version)
        await Migration.async_migrate(hass, entry.entry_id)
    hass.config_entries.async_update_entry(entry, version=1, minor_version=5)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ZendureConfigEntry) -> bool:
    """Set up Zendure as config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    manager = ZendureManager(hass, entry)
    await manager.loadDevices()
    entry.runtime_data = manager
    await manager.async_config_entry_first_refresh()
    entry.async_on_unload(entry.add_update_listener(update_listener))
    return True


async def update_listener(_hass: HomeAssistant, entry: ZendureConfigEntry) -> None:
    """Handle options update."""
    _LOGGER.debug("Updating Zendure config entry: %s", entry.entry_id)
    Api.mqttLogging = entry.data.get(CONF_MQTTLOG, False)
    entry.runtime_data.simulation = entry.data.get(CONF_SIM, False)
    entry.runtime_data.update_p1meter(entry.data.get(CONF_P1METER, "sensor.power_actual"))


async def async_unload_entry(hass: HomeAssistant, entry: ZendureConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading Zendure config entry: %s", entry.entry_id)
    result = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if result:
        manager = entry.runtime_data
        if Api.mqttCloud.is_connected():
            Api.mqttCloud.disconnect()
        if Api.mqttLocal.is_connected():
            Api.mqttLocal.disconnect()
        for c in Api.devices.values():
            if c.zendure is not None and c.zendure.is_connected():
                c.zendure.disconnect()
            c.zendure = None
        manager.update_p1meter(None)
        manager.fuseGroups.clear()
        manager.devices.clear()
    return result


async def async_remove_config_entry_device(_hass: HomeAssistant, entry: ZendureConfigEntry, device_entry: dr.DeviceEntry) -> bool:
    """Remove a device from a config entry."""
    manager = entry.runtime_data

    # check for device to remove
    for d in manager.devices:
        if d.name == device_entry.name:
            manager.devices.remove(d)
            return True

        if isinstance(d, ZendureDevice) and (bat := next((b for b in d.batteries.values() if b.name == device_entry.name), None)) is not None:
            d.batteries.pop(bat.deviceId)
            return True

    return True
