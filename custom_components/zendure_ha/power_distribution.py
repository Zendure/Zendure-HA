import logging
import math

from typing import Dict, List, Any
from enum import Enum, auto
from .const import DeviceState, SmartMode

_LOGGER = logging.getLogger(__name__)

_last_active_count = 1  


class MainState(Enum):
    GRID_CHARGE = 5
    GRID_DISCHARGE = 6


class SubState(Enum):
    IDLE = 1
    CHARGE = 3
    DISCHARGE = 4
    BYPASS = 8
    STARTING = 9


class DeviceStateMachine:
    def __init__(self):
        self.main = MainState.GRID_CHARGE
        self.sub = SubState.IDLE

    def __repr__(self):
        return f"<State main={self.main.name} sub={self.sub.name}>"


def decide_substate(device, mainstate: MainState) -> SubState:
    """Bestimme den SubState eines Geräts anhand der Sensordaten."""
    if device.state == DeviceState.OFFLINE:
        return SubState.IDLE

    if device.byPass.is_on:
        return SubState.BYPASS

    # Netz laden
    if mainstate == MainState.GRID_CHARGE:
        if device.pwr_solar > 0 and device.pwr_home_out > 0:
            return SubState.DISCHARGE
        if device.pwr_battery_in > 0:
            return SubState.CHARGE

    # Netz entladen
    if mainstate == MainState.GRID_DISCHARGE:
        if device.pwr_home_out > 0 or device.pwr_battery_out > 0:
            return SubState.DISCHARGE
        if device.pwr_solar > 0 and device.pwr_home_out == 0:
            return SubState.CHARGE

    # IDLE wenn nichts passiert
    if all(v == 0 for v in [
        device.pwr_solar,
        device.pwr_home_out,
        device.pwr_home_in,
        device.pwr_battery_in,
        device.pwr_battery_out
    ]):
        return SubState.IDLE

    return SubState.IDLE


_bypass_lock = False

def handle_bypass(devices: List[Any], soc_release: int = 90) -> Dict[Any, int]:
    """
    Prüft Geräte auf SOCFULL mit gridReverse.
    Gibt Allocation-Einträge zurück für Geräte, die in Bypass gehen.
    Nutzt eine Hysterese:
    - Bypass aktiv, wenn Geräte SOCFULL sind
    - Rückkehr erst, wenn ein Gerät < soc_release % fällt (default: 90)
    """
    global _bypass_lock
    bypass_alloc = {}

    # Check: Gibt es noch Geräte unterhalb SOCFULL?
    has_non_full = any(d.state != DeviceState.SOCFULL for d in devices)

    # Wenn alle voll → Lock setzen
    if not has_non_full and not _bypass_lock:
        if not _bypass_lock:
            _LOGGER.info("Alle Geräte voll → Bypass-Lock gesetzt.")
        _bypass_lock = True

        # alle Flags zurücksetzen
        for d in devices:
            d.is_bypass = False
        _LOGGER.debug("Alle Geräte voll → alle d.is_bypass = False gesetzt.")
        return bypass_alloc  # kein Bypass aktiv solange Lock

    # Wenn Lock aktiv ist, prüfen ob eines <90% gefallen ist
    if _bypass_lock:
        if any(d.soc_lvl < soc_release for d in devices):
            _bypass_lock = False
            _LOGGER.info(f"Bypass-Lock aufgehoben (mind. ein Gerät < {soc_release}%).")
        else:
            _LOGGER.debug("Bypass-Lock aktiv → kein Bypass-Handling.")
            # Flags sauberhalten
            for d in devices:
                d.is_bypass = False
            return bypass_alloc

    # Normaler Bypass-Check
    for d in devices:
        if d.state == DeviceState.SOCFULL:
            if not d.pvaktiv:
                d.is_bypass = False
                _LOGGER.debug(f"{d.name} ist voll, aber pvaktiv=False → kein Bypass.")
                continue
            if d.gridReverse is None:
                d.is_bypass = False
                _LOGGER.debug(f"{d.name} ist voll, aber gridReverse fehlt → kein Bypass.")
                continue

            # Gerät geht in Bypass
            kickstart = 0
            if d.pwr_solar == 0:
                kickstart = 50  # Kickstart

            bypass_alloc[d] = d.pwr_solar + kickstart
            d.is_bypass = True
            _LOGGER.info(
                f"{d.name} ist voll → Bypass aktiv "
                f"(PV={d.pwr_solar}W, Kick={kickstart}W, GridReverse={d.gridReverse})"
            )
        else:
            d.is_bypass = False

    if not bypass_alloc:
        _LOGGER.debug("Kein Gerät für Bypass gefunden.")

    return bypass_alloc


_soc_protection_active = False

def handle_soc_protect(
    devices: List[Any],
    power_to_devide: int,
    discharge_on: int = 50,
    discharge_off: int = 30,
) -> Dict[Any, int]:
    """
    Aktiviert SOC-Schutz nur, wenn Solar deutlich kleiner als die Last ist.
    - Schutz AUS, wenn total_pv > Last+60
    - Schutz EIN, wenn total_pv <= Last+5
    - Pro Gerät Hysterese für minimale Entladung (50W / 30W)
    """
    global _soc_protection_active
    protect_alloc: Dict[Any, int] = {}

    total_pv = sum(d.pwr_solar for d in devices if d.pwr_solar > 0)
    load = abs(power_to_devide)

    # Hysterese für Aktivierung/Deaktivierung
    if total_pv > load + 60:
        _soc_protection_active = False
        _LOGGER.debug(f"SOC-Schutz AUS: Solar={total_pv}W > Last+60 ({load+60}W)")
    elif total_pv <= load + 5:
        _soc_protection_active = True
        _LOGGER.debug(f"SOC-Schutz EIN: Solar={total_pv}W <= Last+5 ({load+5}W)")

    # Nur wenn Schutz aktiv ist, Geräte markieren
    if _soc_protection_active:
        for d in devices:
            # globale SOC-Logik
            if d.soc_lvl <= d.min_soc:
                d.is_soc_protect = True
            elif d.soc_lvl > d.min_soc + 5:
                d.is_soc_protect = False

            # Hysterese für minimale Entladeleistung
            if getattr(d, "is_soc_protect", False):
                allowed = min(d.pwr_solar, d.limitDischarge, load)

                if not hasattr(d, "soc_helper_active"):
                    d.soc_helper_active = False

                if allowed > discharge_on:
                    if not d.soc_helper_active:
                        _LOGGER.info(f"SOC-Helfer AKTIV für {d.name}: {allowed}W > {discharge_on}W")
                    d.soc_helper_active = True
                elif allowed < discharge_off:
                    if d.soc_helper_active:
                        protect_alloc[d] = 0
                        _LOGGER.info(f"SOC-Helfer AUS für {d.name}: {allowed}W < {discharge_off}W")
                    d.soc_helper_active = False

                if d.soc_helper_active:
                    protect_alloc[d] = allowed
                    _LOGGER.info(
                        f"{d.name} SOC-Schutz aktiv: SOC={d.soc_lvl}%, "
                        f"PV={d.pwr_solar}W, erlaubt={allowed}W"
                    )

    return protect_alloc


def solar_helper(dev, solar_threshold_on=55, solar_threshold_off=35):
    """
    Prüft pro Gerät ob es trotz SOCEMPTY durch Solarleistung aktiviert werden darf.
    Nutzt eine Hysterese: ON > 55W, OFF < 35W.
    """
    # initialisiere, falls das Attribut fehlt
    if not hasattr(dev, "helper_active"):
        dev.helper_active = False

    if dev.pwr_solar > solar_threshold_on:
        if not dev.helper_active:
            _LOGGER.info(f"Helfer AKTIV für {dev.name}: PV={dev.pwr_solar}W > {solar_threshold_on}W")
        dev.helper_active = True
    elif dev.pwr_solar < solar_threshold_off:
        if dev.helper_active:
            _LOGGER.info(f"Helfer AUS für {dev.name}: PV={dev.pwr_solar}W < {solar_threshold_off}W")
        dev.helper_active = False

    return dev.helper_active


_helper_mode_active = False  

def should_use_helpers(needed: int) -> bool:
    """
    Hysterese für Helfer-Geräte:
    - Aktivieren, wenn Restlast > 100W
    - Deaktivieren, wenn Restlast < 30W
    """
    global _helper_mode_active

    if needed > 50 and not _helper_mode_active:
        _helper_mode_active = True
        _LOGGER.debug(f"Helfer aktiviert: Restlast {needed}W > 100W")
    elif needed < 30 and _helper_mode_active:
        _helper_mode_active = False
        _LOGGER.debug(f"Helfer deaktiviert: Restlast {needed}W < 30W")

    return _helper_mode_active


def update_extra_candidate(dev, on_threshold=50, off_threshold=30):
    """
    Aktiviert ein Gerät als extra_candidate nur, wenn genug PV-Leistung da ist.
    Nutzt Hysterese: ON > on_threshold, OFF < off_threshold.
    """
    if not hasattr(dev, "extra_candidate_active"):
        dev.extra_candidate_active = False

    if dev.pwr_solar > on_threshold:
        if not dev.extra_candidate_active:
            _LOGGER.info(f"{dev.name} als Extra-Kandidat AKTIV (PV={dev.pwr_solar}W > {on_threshold}W)")
        dev.extra_candidate_active = True
    elif dev.pwr_solar < off_threshold:
        if dev.extra_candidate_active:
            _LOGGER.info(f"{dev.name} als Extra-Kandidat DEAKTIV (PV={dev.pwr_solar}W < {off_threshold}W)")
        dev.extra_candidate_active = False

    return dev.extra_candidate_active


_last_active_count = 0
_last_order: List[Any] = []

def distribute_power(devices: List[Any], power_to_devide: int, main_state: MainState) -> Dict[Any, int]:

    global _last_order, _last_active_count, _soc_protection_active

    device_snapshots = []
    allocation: Dict[Any, int] = {}

    for dev in devices:
        device_snapshots.append({
            "name": dev.name,
            "pwr_home_in": dev.pwr_home_in,
            "pwr_home_out": dev.pwr_home_out,
            "pwr_batt_in": dev.pwr_battery_in,
            "pwr_batt_out": dev.pwr_battery_out,
            "pwr_solar": dev.pwr_solar,
            "soc_lvl": dev.soc_lvl,
            "max_soc": dev.max_soc,
            "min_soc": dev.min_soc,
            "state": dev.state,
            "limitCharge": dev.limitCharge,
            "limitDischarge": dev.limitDischarge,
            "pvStatus": dev.pvStatus,
            "actualKwh": dev.actualKwh,
            "kWh": dev.kWh,
            "energy_diff_kwh": dev.energy_diff_kwh,
        })

    # Charge
    if main_state == MainState.GRID_CHARGE:
        candidates = [d for d in devices if d.state != DeviceState.SOCFULL and not d.is_bypass and not d.is_hand_bypass]
        candidates_full = [d for d in devices if d.state == DeviceState.SOCFULL and not d.is_bypass and not d.is_hand_bypass]
        for d in candidates_full:
            allocation[d] = 0

    else:  # DISCHARGE
        candidates = [d for d in devices if (d.state != DeviceState.SOCEMPTY or solar_helper(d)) and not d.is_bypass and not d.is_hand_bypass]
        #candidates_empty = [d for d in devices if d.state == DeviceState.SOCEMPTY and not solar_helper(d)]
        #for d in candidates_empty:
        #    allocation[d] = 0

        #soc protection at the morning an Hous priority
        soc_alloc = handle_soc_protect(devices, power_to_devide)
        if _soc_protection_active:
            allocation.update(soc_alloc)
            candidates = [d for d in candidates if not getattr(d, "is_soc_protect", False)]

    if not candidates:
        if allocation:  # enthält die soc_alloc-Werte von handle_soc_protect devices
            return allocation
        return {d: 0 for d in devices}

    # --- alte Reihenfolge wiederherstellen ---
    if _last_order:
        # nur Geräte nehmen, die jetzt auch Kandidaten sind
        ordered = [d for d in _last_order if d in candidates]
        # neue oder wieder aktivierte hinten anhängen
        for d in candidates:
            if d not in ordered:
                ordered.append(d)
        candidates = ordered

    active_count = min(max(1, _last_active_count), len(candidates))

    #Last % bestimmen
    active_devs = candidates[:active_count]
    first = active_devs[0]

    limit = first.limitDischarge if main_state == MainState.GRID_DISCHARGE else abs(first.limitCharge)
    total_limit = sum(d.limitDischarge if main_state == MainState.GRID_DISCHARGE else abs(d.limitCharge) for d in active_devs)
    planned = abs(power_to_devide) * limit / total_limit
    load_pct = (planned / limit) * 100 if limit > 0 else 0

    _LOGGER.debug(
        f"Load-Check: power_to_devide={power_to_devide}, "
        f"active_count={active_count}, "
        f"limit_first_dev={limit}, total_limit={total_limit}, "
        f"planned={planned:.1f}, load_pct={load_pct:.1f}%"
    )

    # 20/60-Regel
    if load_pct > 60 and active_count < len(candidates):
        active_count += 1
        _LOGGER.info("plus count")
    elif load_pct < 20 and active_count > 1:
        #hier das letzte gerät aus candidates[:active_count] auf 0 power setzten
        last_dev = candidates[active_count - 1]
        allocation[last_dev] = 0
        active_count -= 1
        _LOGGER.info(f"{last_dev.name} wurde deaktiviert wegen zu geringer Leistung")

    _LOGGER.info(f"last count {_last_active_count} activecount {active_count}")
    _last_active_count = active_count

    # prüfen und rotieren
    ROTATION_THRESHOLD = 0.10  # 10 % Energie differenz dann rotieren
    if candidates:
        first = candidates[0]
        should_rotate = any(
            abs(d.energy_diff_kwh) >= ROTATION_THRESHOLD * d.kWh
            for d in candidates
        )

    if should_rotate:
        for d in devices:
            d.energy_diff_kwh = 0

        if len(candidates) > active_count:
            allocation[first] = 0
            _LOGGER.info(
                f"{first.name} wurde auf 0 gesetzt, da Rotation stattfand "
                f"und es ein weiteres Gerät gibt zu nutzen!"
            )

        soc_levels = [d.soc_lvl for d in candidates]
        max_soc_lvl = max(soc_levels)
        min_soc_lvl = min(soc_levels)

        if max_soc_lvl - min_soc_lvl > 20:
            # es gibt ein Gerät mit deutlich höherem SOC
            fullest = max(candidates[1:], key=lambda d: d.soc_lvl, default=None)
            if fullest and fullest.soc_lvl >= min_soc_lvl + 20:
                candidates.remove(fullest)
                candidates.insert(0, fullest)
                _LOGGER.info(
                    f"SOC-Trick: {fullest.name} (SOC={fullest.soc_lvl}%) "
                    f"vor {first.name} (SOC={first.soc_lvl}%) gesetzt."
                )
        else:
            # alle liegen nah beieinander
            candidates.append(candidates.pop(0))
            _LOGGER.debug("Normale Rotation, da SOC-Differenz ≤ 20%")

    # Aktive Geräte festlegen
    active_devs = candidates[:active_count]
    _LOGGER.debug("Aktive Geräte: " + ", ".join(d.name for d in active_devs))

    if main_state == MainState.GRID_DISCHARGE:

        needed = 0
        helper_pwr = 0
        pv_sum_active = sum(d.pwr_solar for d in active_devs)

        if pv_sum_active >= abs(power_to_devide):
            # Aktive Geräte haben genug PV  proportional, begrenzt auf deren PV
            _LOGGER.debug(f"Aktive Geräte decken {pv_sum_active}W PV, genug für {power_to_devide}W")
            total_pv = sum(d.pwr_solar for d in active_devs if d.pwr_solar > 30)
            for d in active_devs:
                share = (d.pwr_solar / total_pv) if total_pv > 0 else 0
                allocation[d] = int(abs(power_to_devide) * share)
        else:
            # Nicht genug PV, prüfen ob andere Geräte PV haben
            needed = abs(power_to_devide) - pv_sum_active
            _LOGGER.debug(f"PV reicht nicht ({pv_sum_active}W), es fehlen {needed}W → suche Extra-PV-Geräte")

            # nur nicht aktive devices scannen + Hysterese berücksichtigen
            extra_candidates = [
                snap for snap in device_snapshots
                if snap["name"] not in [d.name for d in active_devs]
            ]

            # Filter mit Hysterese
            extra_candidates = [
                snap for snap in extra_candidates
                if update_extra_candidate(next(d for d in devices if d.name == snap["name"]))
            ]

            # sortiere nach größter PV-Leistung
            extra_candidates.sort(key=lambda x: x["pwr_solar"], reverse=True)

            for snap in extra_candidates:
                dev = next((d for d in candidates if d.name == snap["name"]), None)
                if dev is None:
                    _LOGGER.warning(f"Kein Kandidat gefunden für Snapshot {snap['name']}, überspringe.")
                    continue
                if not should_use_helpers(needed):  # Abbruch, wenn fast gedeckt
                    break
                take = min(snap["pwr_solar"], needed, dev.limitDischarge)
                allocation[dev] = take
                helper_pwr += take
                needed -= take

                dev.is_solar_helper = True 

                _LOGGER.info(f"Zusatzgerät {dev.name}: {take}W direkt aus PV genutzt (PV={snap['pwr_solar']}W, Restbedarf={needed}W)")

            #Leistung vertielen
            power_to_devide = power_to_devide - helper_pwr
            total_limit = sum(d.limitDischarge for d in active_devs)
            for d in active_devs:
                allocation[d] = int(abs(power_to_devide) * abs(d.limitDischarge) / total_limit)

        for d in devices:
            if getattr(d, "is_solar_helper", False) and d not in allocation:
                allocation[d] = 0
                d.is_solar_helper = False
                _LOGGER.debug(f"Helfer {d.name} wieder deaktiviert, keine PV-Unterstützung nötig.")

    else:  # GRID_CHARGE
        total_limit = sum(d.limitCharge for d in active_devs)
        for d in active_devs:
            allocation[d] = int(abs(power_to_devide) * abs(d.limitCharge) / abs(total_limit))

    _last_order = candidates.copy()

    bypass_alloc = handle_bypass(devices)
    allocation.update(bypass_alloc)

    _LOGGER.debug("Finale Allocation: " + str({d.name: p for d, p in allocation.items()}))

    return allocation
