"""Distribution: fleet manager."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .button import HaButton
from .const import DOMAIN, PANEL_TITLE, Phase, PhaseModi, watt_factor
from .device import DeviceState, DistributionDevice
from .entity import EntityDevice, snakecase
from .number import HaNumber
from .select import HaSelect
from .sensor import HaSensor
from .switch import HaSwitch


_LOGGER = logging.getLogger(__name__)

RANGE_MIN = 50  # minimal watts
WEAR_DAYS = 7
WEAR_BIAS = 0.6  # wear weight in the order score
PHASE_DAYS = 10  # how far back the detection reads the recorder; raw states live about this long
PHASE_JUMP_W = 50  # an entity update that moves this much is a jump, everything smaller stays in the database
PHASE_STEP_W = 150  # a device event this big is worth matching
PHASE_MERGE_S = 15.0  # device jumps this close together are one event: the ramp of one command
PHASE_BEFORE_S, PHASE_AFTER_S, PHASE_MARGIN_S = 2.0, 10.0, 5.0  # the window around an event, and how long before it the rest must already be quiet
PHASE_MIN_EVENTS, PHASE_SURE_EVENTS = 5, 10  # clean events needed for a verdict, and for applying it
PHASE_SURE, PHASE_OTHER = 0.9, 0.3  # the ratio a device must show on its own phase, and the most the other two may show
PHASE_MARGIN_W = 230  # 1 A at 230 V: a phase at its limit is landed this far under it
REPLAY_HOURS = 24  # the window the export button pulls from the recorder


class DistributionMode(Enum):
    OFF = 0
    MANUAL = 1
    SMART = 2
    SMART_DISCHARGING = 3
    SMART_CHARGING = 4
    STORE_SOLAR = 5


DistributionModi = {m.value: m.name.lower() for m in DistributionMode}  # the select options; the lowercase names are the translation keys


@dataclass
class DistributionProfile:
    """Per-direction settings and device order for the fleet regulator."""

    reverse_ws: int = 3000  # watt-seconds of opposite demand before the fleet reverses to this direction
    last_change: float = 0.0  # timestamp of the last reversal to this direction
    order: list[DistributionDevice] = field(default_factory=list)
    phases: list[list[DistributionDevice]] = field(default_factory=lambda: [[], [], [], []])


class Distribution(EntityDevice):
    """Per-meter fleet manager (active when self.devices is non-empty)."""

    def __init__(self, hass: HomeAssistant, entry_id: str, subentry_id: str | None = None) -> None:
        dev = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry_id, config_subentry_id=subentry_id, identifiers={(DOMAIN, PANEL_TITLE)}, name="Distribution"
        )  # display name only; ids stay on PANEL_TITLE

        # the device migrated from 1.x is still a member of the main entry; take it out so it only belongs to its subentry
        if subentry_id and None in dev.config_entries_subentries.get(entry_id, set()):
            dr.async_get(hass).async_update_device(dev.id, remove_config_entry_id=entry_id, remove_config_subentry_id=None)
        EntityDevice.__init__(self, hass, PANEL_TITLE, subentry_id=subentry_id)
        self.p1_device: str = ""
        self.on_save: Callable | None = None  # set by the coordinator: persists the store
        self.p1_entities: list[list[tuple[str, float]]] = [[], [], [], []]  # set by the coordinator: its p1_entities, indexed on Phase, as (entity_id, signed watt factor)
        self.solar = 0  # W the solar panels make now, set by the coordinator
        self.solar_panels: list[tuple[str, str]] = []  # set by the coordinator: per solar panel its power entity and derate entity
        self.allow_export = True  # entity value: off means the panels are throttled so the meter does not export
        self.max_kw: float = 17.0
        self.max_output: int = 0  # W the fleet may deliver to the house in total; 0 = no cap, 800 for Germany
        self.mqtt_log: bool = False
        self.phase_limit_w = 17000 / 3

        # entity values: the declaration is the initial state, the entity writes it from here on
        self.mode = 0
        self.manual_power = 0.0
        self.setpoint_correction = 0.0

        self.devices: list[DistributionDevice] = []
        self.profile = [DistributionProfile(), DistributionProfile()]
        self.charging = False
        self.reverse_delta = 0.0  # 0 = no reversal running, otherwise watt-seconds left until the flip
        self.p1_time = 0.0
        self.wear_date = 0.0
        self.order_date = 0.0
        self.setpoint_avg = 0.0
        # indexed by Phase: 0 the three-phase devices, 1 to 3 the phases
        self.power: list[float] = [0, 0, 0, 0]  # what the meter shows per phase
        self.sp: list[int] = [0, 0, 0, 0]  # per phase the sum of the setpoints of its devices, index 0 the three-phase ones
        self.available: list[int] = [0, 0, 0, 0]  # per phase what its devices can still deliver, 0 for an empty one
        self.phase_limited = [False, False, False, False]  # per phase whether the phase guard holds it out of the regular distribution
        self.pwr_low = 0.0  # discharging: how long the first running device has sat under 1.5 times its start level, in watt-ticks
        self.setpoint: int = 0
        self.capacity = 0.0

    def entity_create(self) -> None:
        HaSelect(self, "mode", DistributionModi, on_changed=self.update_mode, restore=True)
        HaNumber(self, "manual_power", uom="W", minimum=-10000, maximum=10000, restore=True)
        HaNumber(self, "setpoint_correction", uom="W", minimum=-1000, maximum=1000, restore=True)
        HaSwitch(self, "allow_export", restore=True, default=True)

        # Battery fleet aggregate (sum / capacity-weighted over all units)
        HaSensor(self, "soc", uom="%", device_class="battery", state_class="measurement", precision=1)
        HaSensor(self, "capacity_kwh", uom="kWh", device_class="energy_storage", state_class="measurement", precision=1)
        HaSensor(self, "available_kwh", uom="kWh", device_class="energy_storage", state_class="measurement", precision=1)
        HaSensor(self, "charge_power", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "discharge_power", uom="W", device_class="power", state_class="measurement", aggr=True)

        # System totals from the live P1 telemetry
        HaSensor(self, "house_load", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "house_solar", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "grid_import", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "grid_export", uom="W", device_class="power", state_class="measurement", aggr=True)

        # the last day of P1 and fleet telemetry as a replay file, for tests/replay/replay.py
        HaButton(self, "export_replay", press=self.export_replay)
        HaButton(self, "detect_phases", press=self.detect_phases)  # every device's phase from its power jumps in the recorder

    async def export_replay(self, _entity: Any = None, hours: float = REPLAY_HOURS) -> str | None:
        """Pull the P1 total and every device's power, solar and soc from the recorder into www/, in the replay JSON shape."""
        if not self.p1_entities[Phase.all]:
            persistent_notification.async_create(self.hass, "No P1 meter configured, nothing to export", PANEL_TITLE, f"{DOMAIN}_replay")
            return None
        end = dt_util.utcnow()
        start = end - timedelta(hours=hours)
        recorder = get_instance(self.hass)

        async def series(entity_id: str) -> list[tuple[float, float]]:
            # numeric (timestamp, value) pairs in the window, seeded with the last value before it
            states = await recorder.async_add_executor_job(history.state_changes_during_period, self.hass, start, end, entity_id, True)
            out = []
            for s in states.get(entity_id, []):
                try:
                    out.append((s.last_updated.timestamp(), float(s.state)))
                except (TypeError, ValueError):
                    continue
            return out

        def fill(points: list[tuple[float, float]], timeline: list[float]) -> list[float]:
            # forward-fill a sparse series onto the master timeline; before the first point it is 0
            out, i, last = [], 0, 0.0
            for t in timeline:
                while i < len(points) and points[i][0] <= t:
                    last = points[i][1]
                    i += 1
                out.append(last)
            return out

        # the P1 readings are the master clock; a consumed/produced pair is summed with its signs
        p1_series = [(await series(eid), factor) for eid, factor in self.p1_entities[Phase.all]]
        timeline = sorted({t for points, _ in p1_series for t, _ in points if t >= start.timestamp()})
        if not timeline:
            persistent_notification.async_create(self.hass, f"No P1 readings in the last {hours:.0f} h, nothing to export", PANEL_TITLE, f"{DOMAIN}_replay")
            return None
        p1 = [0.0] * len(timeline)
        for points, factor in p1_series:
            for i, v in enumerate(fill(points, timeline)):
                p1[i] += v * factor

        # the solar panels summed on the same clock, in watts; live this is self.solar, the stabilizer's solar input
        pv = [0.0] * len(timeline)
        for eid in {power_entity for power_entity, _ in self.solar_panels if power_entity}:
            factor = watt_factor(self.hass.states.get(eid))
            for i, v in enumerate(fill(await series(eid), timeline)):
                pv[i] += v * factor

        meta, power, solar, soc = [], {}, {}, {}
        for d in self.devices:
            ids = {key: getattr(d.entities.get(key), "entity_id", None) for key in ("home_output", "home_input", "solar", "soc")}
            if not (ids["home_output"] and ids["home_input"] and ids["soc"]):
                continue
            key = snakecase(d.name)
            meta.append(
                {
                    "key": key,
                    "name": d.name,
                    "model": getattr(d, "model", ""),
                    "kwh": round(d.kwh, 2),
                    "power_discharge": d.power_profile[0].power,
                    "power_charge": d.power_profile[1].power,
                    "soc_min": d.soc_min,
                    "soc_max": d.soc_max,
                }
            )
            # the device contract is one signed value: positive discharges, negative charges
            out = fill(await series(ids["home_output"]), timeline)
            inp = fill(await series(ids["home_input"]), timeline)
            power[key] = [round(a - b) for a, b in zip(out, inp)]
            solar[key] = [round(v) for v in fill(await series(ids["solar"]), timeline)] if ids["solar"] else [0] * len(timeline)
            soc[key] = fill(await series(ids["soc"]), timeline)

        payload = {
            "meta": {
                "source": f"{DOMAIN}-recorder",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "samples": len(timeline),
                "p1_entity": ",".join(e for e, _ in self.p1_entities[Phase.all]),
                "pv_entity": ",".join(power_entity for power_entity, _ in self.solar_panels if power_entity),
            },
            "devices": meta,
            "t": [round(t, 3) for t in timeline],
            "p1": [round(v) for v in p1],
            "pv": [round(v) for v in pv],
            "power": power,
            "solar": solar,
            "soc": soc,
        }
        name = f"{DOMAIN}_replay_{dt_util.now():%Y%m%d_%H%M}.json"
        www = Path(self.hass.config.path("www"))
        existed = await self.hass.async_add_executor_job(www.is_dir)
        await self.hass.async_add_executor_job(lambda: (www.mkdir(exist_ok=True), (www / name).write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")))
        message = f"Replay data written to www/{name}: {len(timeline)} samples over {hours:.0f} h, {len(meta)} devices. Download: /local/{name}"
        if not existed:
            message += ". The www folder was just created: restart Home Assistant once before the link works."
        persistent_notification.async_create(self.hass, message, PANEL_TITLE, f"{DOMAIN}_replay")
        _LOGGER.info("Replay export: %s", message)
        return name

    async def update_mode(self, entity: HaSelect, _operation: Any) -> None:
        mode = DistributionMode(entity.value)
        _LOGGER.info("Update operation: %s", mode)

        if mode != DistributionMode.OFF and (len(self.devices) == 0 or all(d.state == DeviceState.OFFLINE for d in self.devices)):
            _LOGGER.warning("No devices online, not possible to start the operation")
            persistent_notification.async_create(self.hass, "No devices online, not possible to start the operation", PANEL_TITLE, PANEL_TITLE)
            return

        if mode != DistributionMode.OFF and not self.p1_device:
            _LOGGER.warning("No P1 meter configured, the distribution never runs without it")
            persistent_notification.async_create(self.hass, "No P1 meter configured, the distribution never runs without it", PANEL_TITLE, PANEL_TITLE)
            return

        if mode == DistributionMode.OFF:
            for d in self.devices:
                d.power_off()

        # persist the settings right away: a crash or container stop must not lose the mode
        if self.on_save is not None:
            await self.on_save()

    def on_invalidate(self) -> None:
        self.order_date = 0
        self.capacity = sum(0 if d.state <= DeviceState.OFFLINE else d.kwh * (d.soc_max - d.soc_min) / 100 for d in self.devices)
        self.entities["capacity_kwh"].update_value(self.capacity)

    async def _process_p1(self, p1: int, power: list[float], now: float) -> None:
        now = now or time.time()
        self.power = power  # what the meter shows per phase, slot 0 is the three-phase slot and stays 0

        # once a day: the 7-day wear/RTE figures, and every device's phase from its jumps in the recorder
        if now >= self.wear_date:
            self.wear_date = now + 86400
            self.hass.async_create_task(self.update_wear())
            self.hass.async_create_task(self.detect_phases())
        power_actual = self.update_telemetry(p1)

        # rebuild the order every 10 minutes, and right away when a device joins or drops out
        if now >= self.order_date:
            self.order_date = now - now % 600 + 600
            self.update_order()

        # off: the sensors are updated, the fleet is not steered; the tick length feeds every rate below
        if (mode := DistributionMode(int(self.mode))) == DistributionMode.OFF:
            return
        elapsed = min(3.0, now - self.p1_time) if self.p1_time else 1.0
        self.p1_time = now

        # the demand is what the meter shows plus what the fleet delivers; the setpoint follows from mode, stabilizer and direction
        self.setpoint = self.update_setpoint(mode, p1 + power_actual + int(self.setpoint_correction), self.solar, now, elapsed)
        self.setpoint_avg += (abs(self.setpoint) - self.setpoint_avg) * elapsed / 900  # ~15-min EMA of the demand
        self.update_power(self.setpoint < 0, self.setpoint, elapsed)

    def update_telemetry(self, p1: int) -> int:
        """What the fleet does now: the sum, the setpoint per phase in self.sp, the live sensors; returns power_actual."""
        power_actual = 0
        kwh = 0.0
        self.sp = [0, 0, 0, 0]
        self.available = [0, 0, 0, 0]
        for d in self.devices:
            if d.state <= DeviceState.EXTERNAL:
                continue  # not ours: it sits in the meter reading like any other load
            # a bypassing device counts its pass-through as house baseline, never more than it delivers (solar is DC, the
            # output is AC after the inverter), and the off-grid socket as house load
            power_actual += d.power_actual - (min(d.power_actual, d.solar) if d.bypass else 0) + max(0, d.grid_off_power)
            kwh += d.kwh * (d.soc - d.soc_min) / 100
            self.sp[d.power_phase or Phase.all] += d.power_setpoint  # Phase.all is 0, so a device without a known phase lands on the three-phase slot as well
            self.available[d.power_phase or Phase.all] += d.power_profile[False].available_power(d)

        # live display sensors from current telemetry (every P1 tick, regardless of mode)
        entities = self.entities
        entities["grid_import"].update_value(max(0, p1))
        entities["grid_export"].update_value(max(0, -p1))
        entities["discharge_power"].update_value(max(0, power_actual))
        entities["charge_power"].update_value(max(0, -power_actual))
        entities["house_load"].update_value(max(0, p1 + self.solar + power_actual))  # consumption = grid import + solar + fleet delivery
        entities["house_solar"].update_value(self.solar)
        entities["available_kwh"].update_value(kwh)
        entities["soc"].update_value(kwh / self.capacity * 100 if self.capacity else 0)
        return power_actual

    def update_setpoint(self, mode: DistributionMode, setpoint: int, solar: int, now: float, elapsed: float) -> int:
        """The fleet setpoint: manual is the knob, the other modes clamp and cap; idle and a pending reversal hold the fleet at 0."""
        if mode == DistributionMode.MANUAL:
            setpoint = int(self.manual_power)
        else:
            if mode == DistributionMode.SMART_DISCHARGING:
                setpoint = max(setpoint, 0)
            elif mode == DistributionMode.SMART_CHARGING:
                setpoint = min(setpoint, 0)
            elif mode == DistributionMode.STORE_SOLAR:
                setpoint = min(setpoint, 0) if solar > 0 else 0

            # the regulatory cap on what the fleet delivers to the house (Germany: 800 W); the app limit per device is not enough with several devices
            if self.max_output > 0:
                setpoint = min(setpoint, self.max_output)

        # idle holds the fleet at 0; a reversal holds it at 0 while the watt-seconds of opposite demand count down, a flip back to charging within minutes costs up to five times as much
        charging = setpoint < 0
        if abs(setpoint) <= RANGE_MIN:
            return 0
        if charging == self.charging:
            self.reverse_delta = 0.0
            return setpoint
        if self.reverse_delta <= 0:
            factor = 1 if not charging else max(1, 5 - int((now - self.profile[charging].last_change) / 60))
            self.reverse_delta = factor * self.profile[charging].reverse_ws
        else:
            self.reverse_delta -= abs(setpoint) * elapsed
            # counted down: flip the direction
            if self.reverse_delta <= 0:
                self.charging = charging
                self.profile[charging].last_change = now
        return 0

    def update_power(self, charging: bool, setpoint: int, elapsed: float) -> None:
        # the rule: nothing over its limit, nothing held, and the demand fits under 17 kW with 1 A margin per phase; the phase guard below is the exception
        if max(self.power) <= self.phase_limit_w and True not in self.phase_limited and sum(self.power) + sum(self.sp) - setpoint <= 3 * (self.phase_limit_w - PHASE_MARGIN_W):
            self.distribute_power(setpoint, self.profile[charging].order)
            return

        # phase guard, first pass: per phase the room under its limit, 1 A under it, the house on a phase being the meter plus our setpoint there;
        # a phase over the limit is limited until it reads 2 A under, in the band its devices keep what they do, the free phases share the rest
        room = [0, 0, 0, 0]
        total = 0  # room of the free phases together
        sp_all = self.sp[Phase.all]
        for phase in (1, 2, 3):
            room[phase] = int(self.phase_limit_w - PHASE_MARGIN_W - self.power[phase] - self.sp[phase])
            if self.power[phase] < self.phase_limit_w - 2 * PHASE_MARGIN_W:
                self.phase_limited[phase] = False
            if self.power[phase] > self.phase_limit_w:
                self.phase_limited[phase] = True
                setpoint -= min(-room[phase], self.available[phase])  # what its own devices keep taking or have to deliver, 1 A under the limit
                if room[phase] < 0:  # at 0 they are not enough: the three-phase devices give way, a third of theirs lands here
                    sp_all = max(sp_all, self.sp[Phase.all] - 3 * room[phase])
            elif self.phase_limited[phase]:
                setpoint -= self.sp[phase]  # in the band: its devices keep what they do
            else:
                room[phase] = max(0, room[phase])
                total += room[phase]

        # the three-phase devices: limited while they help, released with the last limited phase
        if sp_all != self.sp[Phase.all]:
            self.phase_limited[Phase.all] = True
            self.distribute_power(min(0, sp_all), self.profile[charging].phases[Phase.all])
            setpoint -= min(0, sp_all)
        elif self.phase_limited[Phase.all]:
            self.phase_limited[Phase.all] = True in self.phase_limited[1:]
            if self.phase_limited[Phase.all]:
                setpoint -= self.sp[Phase.all]

        # phase guard, second pass: write per phase over the ranked order filtered on it
        for phase in (1, 2, 3):
            devices = self.profile[charging].phases[phase]
            if self.phase_limited[phase]:
                if self.power[phase] > self.phase_limit_w:
                    self.distribute_power(min(-room[phase], self.available[phase]), devices)  # over: 1 A under the limit by its own devices, as far as they can
                continue
            self.distribute_power(int(setpoint * room[phase] / max(total, -setpoint, 1)), devices)  # its share of the rest pro rata on room, never past it

    def distribute_power(self, setpoint: int, order: list[DistributionDevice]) -> None:
        """The setpoint over the devices that already run this way, weighted by rated power and the room in the pack; a standing one starts at RANGE_MIN when the running ones would pass twice their optimal band."""
        charging = setpoint < 0
        sign = -1 if charging else 1
        demand = abs(setpoint)

        # one pass: who runs this way, with the sums the shares need, and who stands; a device running the other way stops
        running: list[DistributionDevice] = []
        standing: list[DistributionDevice] = []
        weights: dict[DistributionDevice, int] = {}
        rated = optimal = weight = 0
        for d in order:
            if d.power_actual * sign > 0:
                running.append(d)
                p_max = abs(d.power_profile[charging].power)
                rated += p_max
                optimal += p_max // 4
                weights[d] = p_max * ((100 - int(d.soc)) if charging else int(d.soc))
                weight += weights[d]
            else:
                standing.append(d)
                if d.power_actual:
                    d.power(0)

        # what the running set cannot carry inside twice its optimal band has to come from standing devices; the set never gets more than its rating
        excess = max(0, demand - 2 * optimal) if demand > RANGE_MIN else 0
        demand = min(rated, demand)

        # each running device its share, the fullest first when charging and the emptiest first when discharging; capped at its rating and at what the rest cannot carry
        for i, d in enumerate(sorted(running, key=lambda d: d.soc, reverse=charging)):
            p_max = abs(d.power_profile[charging].power)
            share = int(demand * weights[d] / weight) if weight else 0
            weight -= weights[d]
            rated -= p_max
            share = min(share, demand, p_max)
            if rated < demand - share:
                share = min(demand - rated, demand, p_max)
            # discharging with several devices: the first one stops once its share has stayed under 1.5 times its start level for long enough, the set is too wide for the demand
            if not charging and len(running) > 1 and i == 0:
                if (delta := 1.5 * (p_max // 10) - share) > 0:
                    self.pwr_low += delta
                else:
                    self.pwr_low = 0
                if self.pwr_low > p_max // 4:
                    share = 0
            d.power(sign * share)
            demand -= share

        # start standing devices at RANGE_MIN, the emptiest first when charging and the fullest first when discharging, until the excess is covered
        for d in sorted(standing, key=lambda d: d.soc, reverse=not charging):
            if excess <= 0:
                break
            if d.soc_limit == (1 if charging else 2):
                continue
            d.power(sign * RANGE_MIN)
            excess -= 2 * (abs(d.power_profile[charging].power) // 4)
            self.pwr_low = 0

    def update_order(self) -> None:
        """Every 10 minutes: rank the devices per direction; frozen in between."""
        wear_mean = sum(d.wear for d in self.devices) / len(self.devices) if self.devices else 0.0
        demand = max(RANGE_MIN, self.setpoint_avg)
        for charging in (False, True):
            scores: dict[DistributionDevice, float] = {}
            for d in self.devices:
                # a device without a battery works as a plain inverter: without this power command it delivers nothing
                if not d.kwh and d.state > DeviceState.CREATED:
                    d.power(d.power_profile[False].power)
                    continue
                # the device's own verdict overrules the percentage
                if d.soc_limit == (1 if charging else 2):
                    continue
                if d.state <= DeviceState.EXTERNAL or not d.kwh or ((d.bypass or d.soc >= d.soc_max) if charging else (d.soc <= d.soc_min)):
                    continue
                soc = int(d.soc / 10) * 10  # quantized: the ranking only moves on a real SoC difference
                span = d.soc_max - d.soc_min
                head = ((d.soc_max - soc) if charging else (soc - d.soc_min)) / span if span else 0.0
                profile = d.power_profile[charging]
                if not profile.power:  # nothing to give in this direction: the app limit or the model says 0
                    continue
                # fit favors devices whose working band matches the expected demand (small at night, big at high load)
                fit = min(1.0, demand / profile.band_low, profile.band_high / demand)
                scores[d] = head * head * fit / (1.0 + WEAR_BIAS * (d.wear / wear_mean if wear_mean else 0.0))
            order = sorted(scores, key=scores.get, reverse=True)
            # on an equal band the current head stays in front: a swap costs a stop and a cold start for a fit or wear difference
            if (head_now := self.profile[charging].order[0] if self.profile[charging].order else None) in scores and int(head_now.soc / 10) == int(order[0].soc / 10):
                order.remove(head_now)
                order.insert(0, head_now)
            self.profile[charging].order = order
            self.profile[charging].phases = [[d for d in order if (d.power_phase or Phase.all) == phase] for phase in (0, 1, 2, 3)]
        for d in self.devices:
            if d.power_setpoint and d.kwh and d not in self.profile[self.charging].order:
                d.power(0)

    def read_jumps(self, entity_ids: list[str]) -> dict[str, list[tuple[float, float]]]:
        # per entity the moments it jumped more than PHASE_JUMP_W against its previous value, as (time, delta); runs on the recorder's thread
        from homeassistant.components.recorder.db_schema import States, StatesMeta
        from homeassistant.components.recorder.util import session_scope
        from sqlalchemy import Float, cast, func, select

        value = cast(States.state, Float)
        previous = func.lag(value).over(partition_by=States.metadata_id, order_by=States.last_updated_ts)
        rows = (
            select(StatesMeta.entity_id, States.last_updated_ts, value.label("v"), previous.label("prev"))
            .join(StatesMeta, StatesMeta.metadata_id == States.metadata_id)
            .where(StatesMeta.entity_id.in_(entity_ids), States.last_updated_ts > time.time() - PHASE_DAYS * 86400, States.state.not_in(("unknown", "unavailable")))
            .subquery()
        )
        query = select(rows.c.entity_id, rows.c.last_updated_ts, rows.c.v - rows.c.prev).where(func.abs(rows.c.v - rows.c.prev) > PHASE_JUMP_W).order_by(rows.c.last_updated_ts)
        jumps: dict[str, list[tuple[float, float]]] = {eid: [] for eid in entity_ids}
        with session_scope(hass=self.hass, read_only=True) as session:
            for eid, ts, delta in session.execute(query):
                jumps[eid].append((ts, delta))
        return jumps

    async def detect_phases(self, _entity: Any = None) -> None:
        """Button on the distribution, and once a day: every device's phase from its power jumps in the recorder; the matching runs on the recorder's thread, the result is applied here."""
        devices = [d for d in self.devices if d.entities.get("home_output") and d.entities.get("home_input")]
        verdicts, last = await get_instance(self.hass).async_add_executor_job(self.match_phases, devices)

        # a sure phase with enough events is applied, set or changed; the rest is advice, all in one notification
        lines = []
        for d in devices:
            n, ratio = last[d]
            shown = f"{n} events, L1 {ratio[1]:.2f} L2 {ratio[2]:.2f} L3 {ratio[3]:.2f}"
            if (phase := verdicts.get(d)) is None:
                lines.append(f"{d.name}: no phase ({shown}), set {PhaseModi[d.power_phase]}")
            elif n < PHASE_SURE_EVENTS:
                lines.append(f"{d.name}: looks like {phase.name} ({shown}), set {PhaseModi[d.power_phase]}, check")
            elif d.power_phase == phase:
                lines.append(f"{d.name}: {phase.name} confirmed ({shown})")
            else:
                lines.append(f"{d.name}: {phase.name} ({shown}), changed from {PhaseModi[d.power_phase]}")
                d.entities["power_phase"].update_value(phase)
        persistent_notification.async_create(self.hass, "\n".join(lines), f"{PANEL_TITLE} phases", f"{DOMAIN}_phases")
        if self.on_save is not None:
            await self.on_save()


    def match_phases(self, devices: list[DistributionDevice]) -> tuple[dict[DistributionDevice, Phase], dict[DistributionDevice, tuple[int, list[float]]]]:
        # every device's power jumps matched to the phase that jumps with them, in rounds: a device with a known phase is subtracted, every device found joins the known. Runs on the recorder's thread.
        roof = [eid for eid, _ in self.solar_panels if eid]
        meter = [eid for phase in (Phase.l1, Phase.l2, Phase.l3) for eid, _ in self.p1_entities[phase]]
        ids = meter + roof + [d.entities[key].entity_id for d in devices for key in ("home_output", "home_input")]
        jumps = self.read_jumps(ids)
        roof_jumps = sorted(j for eid in roof for j in jumps[eid])

        # per device its jumps as one signed series (output up is +, input up is -), merged into events along the ramp of one command
        moves, events = {}, {}
        for d in devices:
            moves[d] = sorted([(t, v) for t, v in jumps[d.entities["home_output"].entity_id]] + [(t, -v) for t, v in jumps[d.entities["home_input"].entity_id]])
            events[d] = []
            for t, v in moves[d]:
                if events[d] and t - events[d][-1][1] <= PHASE_MERGE_S:
                    events[d][-1][1], events[d][-1][2] = t, events[d][-1][2] + v
                else:
                    events[d].append([t, t, v])

        # rounds: a device with a set phase is known from the start and is still judged itself; an unknown device with a sure ratio joins the known
        known = {d: d.power_phase for d in devices if d.power_phase is not None}
        verdicts: dict[DistributionDevice, Phase] = {}
        last: dict[DistributionDevice, tuple[int, list[float]]] = {}
        while True:
            new = {}
            for d in devices:
                if d in verdicts:
                    continue
                # a clean event: neither the roof nor an unknown device moved in its window; a known device that moved gives its jump back to its own phase
                blockers = [roof_jumps] + [moves[o] for o in devices if o is not d and o not in known]
                others = [(moves[o], phase) for o, phase in known.items() if o is not d]
                clean = []
                for t0, t1, dv in events[d]:
                    lo, hi = t0 - PHASE_BEFORE_S, t1 + PHASE_AFTER_S
                    if abs(dv) < PHASE_STEP_W or any(lo - PHASE_MARGIN_S <= t <= hi for rows in blockers for t, _ in rows):
                        continue
                    dp = [0.0] + [sum(factor * v for eid, factor in self.p1_entities[phase] for t, v in jumps[eid] if lo <= t <= hi) for phase in (Phase.l1, Phase.l2, Phase.l3)]
                    for rows, phase in others:
                        dp[phase] += sum(v for t, v in rows if lo - PHASE_MARGIN_S <= t <= hi)
                    clean.append((dv, dp))
                # the phase that follows the device one to one: a ratio near -1 there and near 0 on the other two
                total = sum(dv * dv for dv, _ in clean) or 1.0  # no clean events: ratios of 0, no verdict
                ratio = [sum(dv * dp[n] for dv, dp in clean) / total for n in range(4)]
                last[d] = (len(clean), ratio)
                best, second = sorted((Phase.l1, Phase.l2, Phase.l3), key=lambda p: ratio[p])[:2]
                if len(clean) >= PHASE_MIN_EVENTS and ratio[best] <= -PHASE_SURE and ratio[second] >= -PHASE_OTHER:
                    new[d] = best
            if not new:
                break
            verdicts.update(new)
            known.update({d: phase for d, phase in new.items() if d not in known})

        return verdicts, last

    async def update_wear(self) -> None:
        """Daily: 7-day pack throughput per device -> wear (cycles/week) + RTE sensor."""
        start = dt_util.now() - timedelta(days=WEAR_DAYS)
        for d in self.devices:
            sensors = {key: d.entities[key].entity_id for key in ("battery_input", "battery_output", "soc")}
            stats = await get_instance(self.hass).async_add_executor_job(statistics_during_period, self.hass, start, None, set(sensors.values()), "day", None, {"mean"})

            # day mean (W) x 24h -> kWh, summed over the window
            kwh_in = sum(row["mean"] or 0 for row in stats.get(sensors["battery_input"], [])) * 24 / 1000
            kwh_out = sum(row["mean"] or 0 for row in stats.get(sensors["battery_output"], [])) * 24 / 1000
            d.wear = (kwh_in + kwh_out) / (2 * d.kwh) if d.kwh else 0.0  # equivalent full cycles per week

            # RTE = out / in, corrected for the SoC shift over the window
            if kwh_in > 0 and (soc := stats.get(sensors["soc"], [])):
                soc_shift = ((soc[-1]["mean"] or 0) - (soc[0]["mean"] or 0)) / 100 * d.kwh
                d.entity_update("rte", max(0.0, min(100.0, (kwh_out + soc_shift) / kwh_in * 100)))
