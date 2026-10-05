from __future__ import annotations

import importlib
import logging

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor", "number", "binary_sensor", "select", "switch", "button", "time"]


async def async_migrate_entry(hass, entry) -> bool:
    # version 1 is the zendure_ha 1.x entry: its settings, devices and entity ids move to the v2 shape
    if entry.version == 1:
        from .migration import async_migrate

        await async_migrate(hass, entry)
    return True


async def async_setup_entry(hass, entry) -> bool:
    # Import in the executor, not on the loop: paho-mqtt/bleak do blocking I/O at import time.
    module = await hass.async_add_import_executor_job(importlib.import_module, ".coordinator", __package__)

    # Init the platforms  must be wired before Coordinator.__init__ registers Distribution's entities.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator = module.Coordinator(hass, entry)
    await coordinator.async_setup()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return True


async def async_unload_entry(hass, entry) -> bool:
    coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if coordinator is not None:
        await coordinator.unload()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
