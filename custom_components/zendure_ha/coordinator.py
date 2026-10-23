"""Coordinator: the driver and the distribution from the config entry, the devices from its subentries, the P1 meter and the store."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import timedelta
from types import MappingProxyType
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import Event, HomeAssistant, callback as ha_callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import DOMAIN, PANEL_TITLE, Phase, watt_factor
from .distribution import Distribution
from .entity import EntityDevice, unique_name
from .zendure.driver import ZendureDriver

_LOGGER = logging.getLogger(__name__)

# per integration how its grid total is read: ("enum", power fragment, direction fragment, the state that means export),
# or a list of (name fragment, sign) summed as one signed net; absent means the default produced/consumed name rules
P1_PROFILES: dict[str, Any] = {
    "solaredge": ("enum", "grid_power", "grid_flow_direction", "export"),
    "enphase_envoy": [("net_consumption", 1)],
    "sma": [("metering_power_absorbed", 1), ("metering_power_supplied", -1)],
}
PHASE_FRAGS = ((), ("phase_1", "phase1", "_l1", "phase_a"), ("phase_2", "phase2", "_l2", "phase_b"), ("phase_3", "phase3", "_l3", "phase_c"))  # indexed on Phase: the name fragments that put a P1 entity on that phase


def phase_of(eid: str) -> Phase:
    # the phase a P1 entity name points at; Phase.all is a grid total
    return next((p for p in (Phase.l1, Phase.l2, Phase.l3) if any(f in eid for f in PHASE_FRAGS[p])), Phase.all)


class Coordinator:
    # The one coordinator: driver + distribution from the entry data, devices from the subentries.

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.entry_id = entry.entry_id
        self.devices: list[EntityDevice] = []

        # the distribution lives in its own subentry so its device and entities sit on their own card; created once for entries that predate it
        if (sub := next((s for s in entry.subentries.values() if s.subentry_type == "distribution"), None)) is None:
            sub = ConfigSubentry(data=MappingProxyType({}), subentry_type="distribution", title="Distribution", unique_id="distribution")
            hass.config_entries.async_add_subentry(entry, sub)
        self.distribution = Distribution(hass, self.entry_id, sub.subentry_id)
        self.distribution.on_save = self._store_save
        self.store = Store(hass, version=1, key=f"{DOMAIN}.devices", atomic_writes=True)
        self.payload: dict[str, Any] = {}  # the Store contents: the restorable entity states and power_phase per device
        self.zendure = ZendureDriver(hass, self.entry_id, self.devices, self.distribution)
        self.zendure.on_discovered = self.discovered
        self.zendure.on_zeroconf = self.zeroconf
        self._unsubs: list = []  # registrations for the life of the coordinator: the store timer and the P1 meter
        self.p1_entities: list[list[tuple[str, float]]] = [[], [], [], []]  # indexed on Phase: the meter entities of the total (0) and each phase, one net or a consumed/produced pair, as (entity_id, signed watt factor)
        self.p1_direction = ("", "")  # the direction enum entity and its export state, for a meter that reports the total as a magnitude (SolarEdge)
        self._settings: dict[str, Any] = {}  # the entry data as last applied, to tell a settings change from a subentry change
        self.zeroconf_ip: dict[str, str] = dict(entry.data.get("zeroconf", {}))  # serial -> ip from mDNS for devices that are not built yet

    async def async_setup(self) -> None:
        self.devices.clear()
        self.devices.append(self.distribution)
        self.apply_settings()
        self.payload = await self.store.async_load() or {}
        self.distribution.device_load(self.payload.setdefault("devices", {}).setdefault(self.distribution.device_id, {}))
        self.sync_subentries()
        self.entry.async_on_unload(self.entry.add_update_listener(self._entry_updated))
        await self.zendure.connect()

        # periodic dump: without it the kWh counters only reach the Store when a user happens to move a knob
        self._unsubs.append(async_track_time_interval(self.hass, self._store_save, timedelta(minutes=15)))
        if self.distribution.p1_device:  # every power entity on the P1 device feeds total/L1/L2/L3 by name
            self._track_p1(self.distribution.p1_device)
        self._track_solar()

    def apply_settings(self) -> None:
        # the entry data holds one section per object: "distribution" and "zendure"
        data = self.entry.data
        for key, value in data.get("distribution", {}).items():
            setattr(self.distribution, key, value)
        for key, value in data.get(ZendureDriver.id, {}).items():
            setattr(self.zendure, key, value)
        self.distribution.phase_limit_w = self.distribution.max_kw * 1000 / 3
        self._settings = {k: dict(v) for k, v in data.items() if isinstance(v, dict)}

    def sync_subentries(self) -> None:
        # the devices mirror the entry's subentries: build what is new, drop what is gone. A subentry's data is the driver's business.
        built = {d.subentry_id: d for d in self.devices if d.subentry_id}
        for sid, sub in self.entry.subentries.items():
            if sid in built or sub.subentry_type != ZendureDriver.id:
                continue
            if (device := self.zendure.init_device(sid, sub.title, dict(sub.data))) is None:
                _LOGGER.warning("Subentry %s (%s) holds no device the driver knows", sub.title, sid)
                continue
            device.on_config_changed = self.persist_device
            device.device_load(self.payload.setdefault("devices", {}).setdefault(device.device_id, {}))
            if (ip := self.zeroconf_ip.pop(getattr(device, "sn", ""), None) or self.zeroconf_ip.pop(getattr(device, "device_key", ""), None)) and not getattr(device, "ip", None):
                device.ip = ip
                self.persist_device(device)
        for sid, device in built.items():
            if sid not in self.entry.subentries:
                self.zendure.remove_device(device)  # HA already cleared its registry device and entities along with the subentry
        self.distribution.solar_panels = [(str(sub.data.get("power_entity") or ""), str(sub.data.get("derate_entity") or "")) for sub in self.entry.subentries.values() if sub.subentry_type == "solar_panel"]

    async def _entry_updated(self, _hass: HomeAssistant, entry: ConfigEntry) -> None:
        # HA's own dialogs and our discovery both land here: new subentries become devices, changed settings reconnect the driver
        self.sync_subentries()
        if {k: dict(v) for k, v in entry.data.items() if isinstance(v, dict)} != self._settings:
            self.apply_settings()
            await self.zendure.connect()
            if self.distribution.p1_device:
                self._track_p1(self.distribution.p1_device)
            self._track_solar()

    def discovered(self, data: dict[str, Any]) -> None:
        # the driver found a device: it becomes a subentry with a name that never changes again; the update listener builds it from there
        sn = data["sn"]
        if any(s.data.get("sn") == sn for s in self.entry.subentries.values()):
            return
        family = data.pop("family")
        taken = {s.title for s in self.entry.subentries.values()} | {d.name for d in self.devices}
        name = unique_name(family, sn, taken)
        if (ip := self.zeroconf_ip.pop(sn, None)) and not data.get("ip"):
            data["ip"] = ip
        _LOGGER.info("New Zendure device %s: %s", name, sn)
        self.hass.config_entries.async_add_subentry(self.entry, ConfigSubentry(data=MappingProxyType(data), subentry_type=ZendureDriver.id, title=name, unique_id=f"{ZendureDriver.id}_{sn}"))

    def persist_device(self, device: EntityDevice) -> None:
        # ip, blemac and the pack names back into the device's subentry; a no-op when nothing changed
        if (sub := self.entry.subentries.get(device.subentry_id or "")) is None:
            return
        data = {**sub.data, **device.subentry_data()}
        if dict(sub.data) != data:
            self.hass.config_entries.async_update_subentry(self.entry, sub, data=data)

    def zeroconf(self, find: dict[str, Any]) -> None:
        # the driver's mDNS find: the ip of a device we know, or kept until the device shows up through its own transport
        for d in self.zendure.devices.values():
            if d.sn == find["sn"] or (find["device_id"] and d.device_key == find["device_id"]):
                if (reader := find.get("reader_sn")) and getattr(d, "reader_sn", "") != reader:
                    d.reader_sn = reader
                if getattr(d, "ip", None) != find["ip"]:
                    d.ip = find["ip"]
                self.persist_device(d)  # a no-op when nothing changed
                return
        self.zeroconf_ip[find["sn"] or find["device_id"]] = find["ip"]  # a find without a serial is only matchable on its MQTT address

    async def unload(self) -> None:
        await self._store_save()
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        # stop the fleet and hand HEMS back, so no device keeps running on our last setpoint once we are gone
        for d in self.distribution.devices:
            d.power_off()
        for d in self.devices:
            d.unload()
        await self.zendure.unload()

    async def _store_save(self, _now=None) -> None:
        # the devices key is replaced as a whole, so removed devices drop out; every other setting in the payload stays.
        devices = {}
        for d in self.devices:
            d.device_save(devices.setdefault(d.device_id, {}))
        self.payload["devices"] = devices
        await self.store.async_save(self.payload)

    def track(self, entity_id: str, callback: Callable[[float], None], factor: float | None = None, phase: Phase = Phase.all) -> None:
        # one entity-event registration; parses the state to float (x factor) so the caller only implements the value handler.
        # Without a factor the unit decides: kW becomes watts, and a current is converted with the voltage that follows from the phase.
        if factor is None:
            factor = watt_factor(self.hass.states.get(entity_id), phase)

        @ha_callback
        def handler(event: Event) -> None:
            new = event.data.get("new_state")
            if new is None:
                return
            try:
                value = float(new.state) * factor
            except (ValueError, TypeError):
                return
            callback(value)

        if entity_id != "":
            self._unsubs.append(async_track_state_change_event(self.hass, [entity_id], handler))

    def _track_solar(self) -> None:
        # every panel's power entity feeds one sum on the distribution: what the panels make right now
        solar: dict[str, float] = {}

        def solar_sum(eid: str, watts: float) -> None:
            solar[eid] = watts
            self.distribution.solar = int(sum(solar.values()))

        for eid in {power_entity for power_entity, _ in self.distribution.solar_panels if power_entity}:
            self.track(eid, lambda w, e=eid: solar_sum(e, w))

    def _track_p1(self, device_id: str) -> None:
        # every power entity on the P1 device goes on its phase (0 = the grid total) with a signed watt factor; only the total entities trigger
        entries = er.async_entries_for_device(er.async_get(self.hass), device_id)
        states = getattr(self.hass, "states", None)  # absent offline (tests/tools), then the registry is all there is
        entities = []
        for e in entries:
            attrs = getattr(states.get(e.entity_id) if states else None, "attributes", None) or {}
            unit = attrs.get("unit_of_measurement") or e.unit_of_measurement  # the integration sets both at runtime, so the state comes first
            if (attrs.get("device_class") or e.original_device_class or e.device_class) == "power" or unit in ("W", "kW", "kilowatt", "kilowatts"):
                entities.append((e.entity_id, 1000 if unit in ("kW", "kilowatt", "kilowatts") else 1))
        platform = entries[0].platform if entries else ""
        flip = -1 if platform == "huawei_solar" else 1  # Huawei signs the grid the other way: positive is export

        # the sign of a total: the profile names it, else produced/consumed in the name does, and a net wins over the pair
        totals = [eid for eid, _ in entities if phase_of(eid) == Phase.all]
        has_net = any("produc" not in eid and "consum" not in eid for eid in totals)
        profile = P1_PROFILES.get(platform)
        if profile and profile[0] == "enum":
            # SolarEdge: an unsigned magnitude, its sign comes live from a separate direction enum; without that enum the magnitude is useless
            self.p1_direction = (next((e.entity_id for e in entries if profile[2] in e.entity_id), ""), profile[3])
            profile = [(profile[1], 1 if self.p1_direction[0] else 0)]

        # every entity on its phase with its signed factor; a phase entity is consumed unless its name says produced
        self.p1_entities = [[], [], [], []]
        for eid, factor in entities:
            phase = phase_of(eid)
            if phase != Phase.all:
                sign = -1 if "produc" in eid else 1
            elif profile:
                sign = next((s for frag, s in profile if frag in eid), 0)
            elif "produc" in eid:
                sign = 0 if has_net else -1
            else:
                sign = 0 if has_net and "consum" in eid else 1
            if sign:
                self.p1_entities[phase].append((eid, sign * flip * factor))
        self.distribution.p1_entities = self.p1_entities  # export_replay reads the total entities from it

        # only the total entities trigger; the phases are read along with them
        for eid, _ in self.p1_entities[Phase.all]:
            self.track(eid, self._p1_changed)

        # the regulator never runs without a total: name the meter that gave none
        if not self.p1_entities[Phase.all]:
            persistent_notification.async_create(self.hass, f"P1 meter '{platform}' delivered no usable grid total", PANEL_TITLE, f"{DOMAIN}_p1")

    def _p1_changed(self, _: float) -> None:
        # a grid total entity reported: read the meter, each value its one net entity or consumed and produced added through their signed factors, and hand total and phases to the regulator
        power = [0.0] * 4
        for phase in Phase:
            for eid, factor in self.p1_entities[phase]:
                if (state := self.hass.states.get(eid)) and state.state not in ("unknown", "unavailable"):
                    power[phase] += float(state.state) * factor
        # the total leaves slot 0, which stays 0 for the distribution; a meter that reports a magnitude (SolarEdge) gets its sign from the direction enum
        p1 = power[Phase.all]
        power[Phase.all] = 0.0
        direction, export = self.p1_direction
        if direction and (state := self.hass.states.get(direction)) and state.state == export:
            p1 = -p1
        self.hass.async_create_task(self.distribution._process_p1(int(p1), power, now=time.time()))
