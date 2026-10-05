"""Migration of a zendure_ha 1.x entry: settings into sections, devices into subentries, every entity onto its new name and key."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any

from homeassistant.components.persistent_notification import async_create
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state as rs
from homeassistant.helpers.storage import Store

from .const import DOMAIN, PANEL_TITLE
from .entity import device_by_identifier, snakecase, unique_name
from .zendure.battery import battery_type
from .zendure.driver import ZendureDriver
from .zendure.solarflow import ZendureSolarFlow

_LOGGER = logging.getLogger(__name__)

# old translation_key -> new display key; None: the entity has no successor and is removed from the registry
DEVICE_KEYS: dict[str, str | None] = {
    "output_limit": "output_limit",
    "input_limit": "input_limit",
    "min_soc": "soc_min",
    "soc_set": "soc_max",
    "soc_status": None,
    "soc_limit": "soc_limit",
    "pass": "bypass",
    "fuse_group": None,
    "ac_mode": None,
    "electric_level": "soc",
    "grid_input_power": "home_input",
    "solar_input_power": "solar",
    "output_pack_power": "battery_input",
    "pack_input_power": "battery_output",
    "output_home_power": "home_output",
    "bat_in_out": None,
    "heat_state": "heat_state",
    "hems_state": "hems_state",
    "available_kwh": None,
    "total_kwh": "kwh",
    "connection_status": None,
    "connection": None,
    "ble_adapter": None,
    "remaining_time": "remaining_time",
    "next_calibration": None,
    "aggr_charge": "battery_input_aggr",
    "aggr_discharge": "battery_output_aggr",
    "roundtrip_efficiency": "rte",
    "aggr_grid_input_power": "home_input_aggr",
    "aggr_output_home": "home_output_aggr",
    "aggr_solar": "solar_aggr",
    "aggr_grid_off_power": "grid_off_power_aggr",
    "switch_count": None,
    "mqtt_reset": None,
    "hub_mode": None,
    "ac_switch": None,
    "dc_switch": None,
    "hyper_tmp": "temp",
    "inverse_max_power": "power_max",
}
DEVICE_KEYS |= {snakecase(vendor): name for vendor, (_, name, _) in ZendureSolarFlow.SENSORS.items() if snakecase(vendor) not in DEVICE_KEYS}
BATTERY_KEYS: dict[str, str | None] = {"soc_level": "soc", "pack_state": "pack_state", "state": "state", "power": "power", "max_temp": "max_temp", "total_vol": "total_vol", "batcur": "batcur", "max_vol": "max_vol", "min_vol": "min_vol", "delta_voltage": None}
MANAGER_KEYS: dict[str, str | None] = {"operation": "mode", "manual_power": "manual_power", "available_kwh": "available_kwh", "total_kwh": "capacity_kwh", "global_soc": "soc", "power": None, "operation_state": None}
RESTORED = {"soc_min", "soc_max", "mode", "manual_power"}  # knobs whose last state moves into the Store, next to every kWh counter


class Migration:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.devices = dr.async_get(hass)
        self.entities = er.async_get(hass)
        self.last_states = rs.async_get(hass).last_states
        self.changes: list[tuple[str, str]] = []  # (old entity_id, new entity_id) for the dashboards and automations
        self.store: dict[str, dict[str, Any]] = {}  # device_id -> {unique_id: value} for the v2 Store

    def rename_entities(self, device: dr.DeviceEntry, name: str, device_id: str, keys: dict[str, str | None]) -> None:
        # every entity of the device onto snakecase(f"{name}_{key}"); no successor means the entity goes
        for e in er.async_entries_for_device(self.entities, device.id, include_disabled_entities=True):
            if e.platform != DOMAIN:
                continue
            if (key := keys.get(e.translation_key or "")) is None:
                self.entities.async_remove(e.entity_id)
                continue
            unique_id = snakecase(f"{name}_{key}")
            entity_id = f"{e.domain}.{unique_id}"
            if key in RESTORED or key.endswith("_aggr"):
                if (stored := self.last_states.get(e.entity_id)) is not None and stored.state.state not in ("unknown", "unavailable", ""):
                    value = stored.state.state
                    self.store.setdefault(device_id, {})[unique_id] = value if key == "mode" else float(value)
            if e.entity_id == entity_id and e.unique_id == unique_id:
                continue
            if (other := self.entities.async_get(entity_id)) is not None and other.entity_id != e.entity_id:
                self.entities.async_remove(entity_id)
            try:
                self.entities.async_update_entity(e.entity_id, new_unique_id=unique_id, new_entity_id=entity_id)
            except ValueError as err:
                _LOGGER.warning("Migration: %s could not become %s: %s", e.entity_id, entity_id, err)
                continue
            if e.entity_id != entity_id:
                self.changes.append((e.entity_id, entity_id))

    def migrate_devices(self) -> list[ConfigSubentry]:
        subentries: list[ConfigSubentry] = []
        taken: set[str] = set()
        registered = dr.async_entries_for_config_entry(self.devices, self.entry.entry_id)
        for device in registered:
            if device.via_device_id is not None or not device.model_id or not any(i[0] == DOMAIN for i in device.identifiers):
                continue
            sn = device.serial_number or ""
            ids = [i[1] for i in device.identifiers if i[0] == DOMAIN and i[1] and i[1] != sn]
            device_id = device.hw_version if device.hw_version in ids else (ids[0] if ids else "")
            if not device_id or not sn or (family := ZendureDriver.device_family({"product": device.model})) is None:
                _LOGGER.warning("Migration: %s (%s) has no serial or an unknown product %s, left as it is", device.name, device_id, device.model)
                continue
            name = unique_name(family, sn, taken)
            taken.add(name)
            blemac = next((c[1] for c in device.connections if c[0] == dr.CONNECTION_BLUETOOTH), "")
            batteries: dict[str, str] = {}
            for pack in registered:
                if pack.via_device_id != device.id or not pack.serial_number:
                    continue
                model, _ = battery_type(pack.serial_number)
                pack_name = unique_name(model, pack.serial_number, taken)
                taken.add(pack_name)
                batteries[pack.serial_number] = pack_name
                self.devices.async_update_device(pack.id, name=pack_name, name_by_user=None, new_identifiers={(DOMAIN, f"{model}_{pack.serial_number[-8:]}")}, serial_number=None, hw_version=None)
                self.rename_entities(pack, pack_name, f"{model}_{pack.serial_number[-8:]}", BATTERY_KEYS)
            self.devices.async_update_device(device.id, name=name, name_by_user=None, new_identifiers={(DOMAIN, sn)}, serial_number=None, hw_version=None)
            self.rename_entities(device, name, sn, DEVICE_KEYS)
            data = {"product": device.model, "sn": sn, "device_id": device_id, "prodkey": device.model_id, "ip": "", "blemac": blemac, "batteries": batteries}
            subentries.append(ConfigSubentry(data=MappingProxyType(data), subentry_type=ZendureDriver.id, title=name, unique_id=f"{ZendureDriver.id}_{sn}"))
            _LOGGER.info("Migration: %s -> %s (%s)", device.name, name, sn)

        if (manager := device_by_identifier(self.hass, "Zendure Manager")) is not None:
            self.devices.async_update_device(manager.id, name=PANEL_TITLE, name_by_user=None, new_identifiers={(DOMAIN, PANEL_TITLE)}, serial_number=None, hw_version=None)
            self.rename_entities(manager, PANEL_TITLE, PANEL_TITLE, MANAGER_KEYS)
        return subentries

    def settings(self) -> dict[str, Any]:
        old = self.entry.data
        p1 = self.entities.async_get(str(old.get("p1meter", ""))) if old.get("p1meter") else None
        local = bool(old.get("mqttlocal"))
        return {
            "distribution": {"max_kw": 17.0, "mqtt_log": bool(old.get("mqttlog", False)), "p1_device": (p1.device_id if p1 else None) or ""},
            "zendure": {"apptoken": str(old.get("token", "")), "mqtt_host": str(old.get("mqttserver", "")) if local else "", "mqtt_port": int(old.get("mqttport", 1883) or 1883), "mqtt_user": str(old.get("mqttuser", "")) if local else "", "mqtt_psw": str(old.get("mqttpsw", "")) if local else "", "wifi_ssid": str(old.get("wifissid", "")), "wifi_psw": str(old.get("wifipsw", ""))},
            "zeroconf": {},
        }

    def rewrite_files(self) -> bool:
        # the old entity ids in dashboards, automations and the energy dashboard; runs in the executor
        modified = False
        config_dir = Path(self.hass.config.config_dir)
        storage = config_dir / ".storage"
        paths = [p for p in storage.iterdir() if any(p.name.startswith(f) for f in ("core.automation", "lovelace", "energy"))] if storage.is_dir() else []
        paths += [p for p in config_dir.rglob("*") if p.is_file() and p.suffix in (".yaml", ".json") and not any(part.startswith(".") for part in p.relative_to(config_dir).parts)]
        for path in paths:
            try:
                content = path.read_text(encoding="utf-8")
                updated = self.replace(content)
                if updated != content:
                    path.write_text(updated, encoding="utf-8")
                    modified = True
            except Exception as err:
                _LOGGER.error("Migration: could not rewrite %s: %s", path, err)
        return modified

    def rewrite_entries(self) -> None:
        # template helpers and other integrations that hold one of our entity ids in their data or options
        def change(data: Any) -> bool:
            changed = False
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, (dict, list)):
                        changed |= change(value)
                    elif isinstance(value, str) and (new := self.replace(value)) != value:
                        data[key] = new
                        changed = True
            elif isinstance(data, list):
                for i, item in enumerate(data):
                    if isinstance(item, (dict, list)):
                        changed |= change(item)
                    elif isinstance(item, str) and (new := self.replace(item)) != item:
                        data[i] = new
                        changed = True
            return changed

        for other in self.hass.config_entries.async_entries():
            if other.entry_id == self.entry.entry_id:
                continue
            data, options = dict(other.data or {}), dict(other.options or {})
            if change(data) | change(options):
                self.hass.config_entries.async_update_entry(other, data=data, options=options)
                self.hass.async_create_task(self.hass.config_entries.async_reload(other.entry_id))

    def replace(self, text: str) -> str:
        for old_id, new_id in self.changes:
            text = re.sub(rf"(?<!\w){re.escape(old_id)}(?!\w)", new_id, text)
        return text


async def async_migrate(hass: HomeAssistant, entry: ConfigEntry) -> None:
    m = Migration(hass, entry)
    settings = m.settings()
    subentries = m.migrate_devices()

    # the kWh counters and the knobs continue from their last state, in the Store the v2 devices restore from
    store = Store(hass, 1, f"{DOMAIN}.devices", atomic_writes=True)
    payload = await store.async_load() or {}
    for device_id, values in m.store.items():
        payload.setdefault("devices", {}).setdefault(device_id, {}).update(values)
    await store.async_save(payload)
    await Store(hass, 1, f"{DOMAIN}.storage").async_remove()  # the 1.x device list cache

    hass.config_entries.async_update_entry(entry, data=settings, title=PANEL_TITLE, unique_id=PANEL_TITLE, version=2, minor_version=1)
    subentries.insert(0, ConfigSubentry(data=MappingProxyType({}), subentry_type="distribution", title="Distribution", unique_id="distribution"))  # the fleet control gets its own card
    for sub in subentries:
        hass.config_entries.async_add_subentry(entry, sub)

    if m.changes:
        m.rewrite_entries()
        if await hass.async_add_executor_job(m.rewrite_files):
            async_create(hass, f"Zendure v2 renamed {len(m.changes)} entities. Restart Home Assistant so every dashboard and automation uses the new entity ids.", title="Zendure migration", notification_id="zendure_migration")
    _LOGGER.info("Zendure migration to v2 complete: %d devices, %d entity changes", len(subentries), len(m.changes))
