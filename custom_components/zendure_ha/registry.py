"""Device registry helpers that work across Home Assistant versions.

HA 2026.8 deprecated `DeviceRegistry.async_get_device(identifiers=...)` (removal in
2027.8) in favour of the config-entry scoped `async_get_device_by_identifier`, and
the `via_device` device info key in favour of `via_device_id`. The integration still
supports HA 2025.4 (see hacs.json), so these helpers use the new API when it exists
and fall back to the old one otherwise.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN


def async_get_zendure_device(hass: HomeAssistant, identifier: str) -> dr.DeviceEntry | None:
    """Return the device registered with the identifier (DOMAIN, identifier), if any."""
    device_registry = dr.async_get(hass)
    if not hasattr(device_registry, "async_get_device_by_identifier"):
        # HA < 2026.8
        return device_registry.async_get_device(identifiers={(DOMAIN, identifier)})

    # Identifiers are unique per config entry. The integration is single_config_entry,
    # so at most one entry can own the device and the lookup is unambiguous.
    for entry in hass.config_entries.async_entries(DOMAIN):
        if (device := device_registry.async_get_device_by_identifier((DOMAIN, identifier), entry.entry_id)) is not None:
            return device
    return None


def set_via_device(hass: HomeAssistant, device_info: MutableMapping[str, Any], parent_identifier: str) -> None:
    """Link a device to its parent device (e.g. a battery pack to its SolarFlow)."""
    # HA >= 2026.8: DeviceInfo is a dataclass and expects the parent's registry id
    has_via_device_id = "via_device_id" in getattr(type(device_info), "__dataclass_fields__", {})
    if has_via_device_id and (parent := async_get_zendure_device(hass, parent_identifier)) is not None:
        device_info["via_device_id"] = parent.id
        return

    # HA < 2026.8 (DeviceInfo is a TypedDict), or the parent is not registered yet
    device_info["via_device"] = (DOMAIN, parent_identifier)
