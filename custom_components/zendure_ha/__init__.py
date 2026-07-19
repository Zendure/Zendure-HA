"""Initialize the Zendure component."""

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .api import Api
from .const import (
    CONF_DEVICE_SN,
    CONF_DEVICES,
    CONF_LOCAL_ONLY,
    CONF_MQTTLOG,
    CONF_P1METER,
    CONF_SIM,
)
from .device import ZendureDevice
from .entity import EntityDevice
from .manager import ZendureConfigEntry, ZendureManager
from .migration import Migration

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.SELECT, Platform.SENSOR, Platform.SWITCH]

_LOGGER = logging.getLogger(__name__)


async def async_migrate_entry(hass: HomeAssistant, entry: ZendureConfigEntry) -> bool:
    """Migrate config entry to new version."""
    if entry.version == 1 and entry.minor_version < 8:
        _LOGGER.info("Migrating Zendure config entry from version %s.%s", entry.version, entry.minor_version)
        await Migration.async_migrate(hass, entry.entry_id)
    hass.config_entries.async_update_entry(entry, version=1, minor_version=8)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ZendureConfigEntry) -> bool:
    """Set up Zendure as config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await EntityDevice.async_load_translations(hass)
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
    ZendureManager.simulation = entry.data.get(CONF_SIM, False)
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
        # Api.devices is class-level state that survives a reload; clear it so the
        # next setup rebuilds the authoritative set from the (cloud or local) device
        # list and a removed device does not linger.
        Api.devices.clear()
    return result


async def async_remove_config_entry_device(hass: HomeAssistant, entry: ZendureConfigEntry, device_entry: dr.DeviceEntry) -> bool:
    """Remove a device from a config entry."""
    manager = entry.runtime_data

    # check for device to remove
    for d in manager.devices:
        if d.name == device_entry.name:
            # A local entry has no cloud account to fall back on, so it can't be left
            # with zero devices. Checked against manager.devices (the live set), not
            # entry.data[CONF_DEVICES], since a failed-to-load device would inflate
            # the persisted count and bypass this guard.
            if entry.data.get(CONF_LOCAL_ONLY) and len(manager.devices) <= 1:
                _LOGGER.error(
                    "Cannot remove %s: it is the only device on this local Zendure entry. Remove the Zendure integration entry instead.",
                    d.name,
                )
                return False

            manager.devices.remove(d)
            Api.devices.pop(d.deviceId, None)
            # For a local entry, also drop it from the stored list so it does not
            # reappear on the next reload. Cloud entries are re-fetched, so leave them.
            if entry.data.get(CONF_LOCAL_ONLY):
                devices = [x for x in entry.data.get(CONF_DEVICES, []) if str(x.get(CONF_DEVICE_SN)) != d.deviceId]
                hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_DEVICES: devices})
            return True

        if isinstance(d, ZendureDevice) and (bat := next((b for b in d.batteries.values() if b.name == device_entry.name), None)) is not None:
            d.batteries.pop(bat.deviceId)
            return True

    return True