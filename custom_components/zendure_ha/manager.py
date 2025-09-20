"""Coordinator for Zendure integration."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import traceback
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from math import sqrt
from typing import Any

from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.providers import homeassistant as auth_ha
from homeassistant.components import bluetooth, persistent_notification
from homeassistant.components.number import NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.loader import async_get_integration

from .api import Api
from .const import CONF_P1METER, DOMAIN, DeviceState, SmartMode
from .device import ZendureDevice, ZendureLegacy
from .entity import EntityDevice
from .fusegroup import FuseGroup
from .number import ZendureRestoreNumber
from .select import ZendureRestoreSelect, ZendureSelect
from .sensor import ZendureSensor
from .binary_sensor import ZendureBinarySensor

SCAN_INTERVAL = timedelta(seconds=90)

_LOGGER = logging.getLogger(__name__)

type ZendureConfigEntry = ConfigEntry[ZendureManager]


class ZendureManager(DataUpdateCoordinator[None], EntityDevice):
    """Class to regular update devices."""

    devices: list[ZendureDevice] = []
    fuseGroups: list[FuseGroup] = []

    def __init__(self, hass: HomeAssistant, entry: ZendureConfigEntry) -> None:
        """Initialize Zendure Manager."""
        super().__init__(hass, _LOGGER, name="Zendure Manager", update_interval=SCAN_INTERVAL, config_entry=entry)
        EntityDevice.__init__(self, hass, "manager", "Zendure Manager", "Zendure Manager")
        self.operation = 0
        self.zero_next = datetime.min
        self.zero_fast = datetime.min
        self.check_reset = datetime.min
        self.p1_stddev_history: deque[int] = deque(maxlen=25)
        self.p1meterEvent: Callable[[], None] | None = None
        self.api = Api()
        self.update_count = 0
        self.total = 0

        # --- Look-ahead / Prädiktiv-Regler ---
        self.lead_s = 1.0          # Auto-Tuning: gemessene Verzögerung [s]
        self.plant_gain = 1.0      # Verhältnis Netzänderung / Soll-Änderung

        # Messglättung & Puffer
        self._p1_ema = None
        self._alpha = 0.25         # EMA-Gewicht (0.15–0.35 üblich)
        self._p1_buf = deque(maxlen=30)   # ~1.5 s Verlauf (bei ~20 Hz)

        # Deadband & Rampenlimit
        self.DEADBAND_W = 50       # Grundhysterese ±50 W
        self.RAMP_STEP_W = 100     # max. Zieländerung pro Update
        self._last_target = 0

        # (empfohlen) History-Fenster etwas größer:
        self.p1_history     = deque([25, -25], maxlen=20)   # statt 8
        self.power_history  = deque(maxlen=12)             # statt 5

        # --- Auto-Tuning ---
        self.autotune_enabled = True
        self.tune_interval = timedelta(hours=12)
        self.last_tune = datetime.min
        self._tune_capture = False
        self._tune_samples = []     # list[(datetime, int)]

    def _ema(self, x: int) -> int:
        self._p1_ema = x if self._p1_ema is None else int(self._alpha * x + (1 - self._alpha) * self._p1_ema)
        return self._p1_ema

    def _is_quiet(self) -> bool:
        if len(self.p1_stddev_history) < 10 or len(self.p1_history) < 5:
            return False
        std = sum(self.p1_stddev_history) / len(self.p1_stddev_history)
        avgabs = sum(abs(x) for x in self.p1_history) / len(self.p1_history)
        return std < 20 and avgabs < 50

    def _predict_p1(self) -> tuple[int | None, float]:
        """Look-ahead: Trend auf lead_s Sekunden fortschreiben. 
        Rückgabe: (p1_hat, slope[W/s])"""
        if len(self._p1_buf) < 5:
            return None, 0.0
        t0 = self._p1_buf[0][0]
        xs = [ (t - t0).total_seconds() for (t, _) in self._p1_buf ]
        ys = [ p for (_, p) in self._p1_buf ]
        n  = len(xs)
        mx, my = sum(xs)/n, sum(ys)/n
        num = sum((x - mx)*(y - my) for x, y in zip(xs, ys))
        den = sum((x - mx)**2 for x in xs) or 1e-9
        slope = num / den                            # W pro Sekunde
        p_now = ys[-1]
        p1_hat = int(p_now + slope * self.lead_s)    # Prognose in lead_s s
        return p1_hat, slope

    def _step_feedforward(self, horizon_s: float = 0.40, thresh_w: int = 120) -> int:
        """Erkennt Lastsprung innerhalb ~0.4 s und gibt Gegenstufe zurück."""
        if len(self._p1_buf) < 2:
            return 0
        t_now, p_now = self._p1_buf[-1]
        # Wert ~horizon_s zuvor
        p_then = p_now
        for t, p in reversed(self._p1_buf):
            if (t_now - t).total_seconds() >= horizon_s:
                p_then = p
                break
        jump = p_now - p_then
        if abs(jump) >= thresh_w:
            return int(0.7 * jump)  # leicht unterkompensieren
        return 0

    def _apply_deadband_and_ramp(self, target: int) -> int:
        std = int(getattr(self, "p1_stddev", None).asNumber) if hasattr(self, "p1_stddev") else 0
        band = max(self.DEADBAND_W, 2 * std)
        # Deadband: innerhalb ±band nichts ändern
        if abs(target) <= band:
            self._last_target = 0
            return 0
        # Rampenlimit:
        delta = target - self._last_target
        if   delta >  self.RAMP_STEP_W: target = self._last_target + self.RAMP_STEP_W
        elif delta < -self.RAMP_STEP_W: target = self._last_target - self.RAMP_STEP_W
        self._last_target = target
        return target

    async def run_autotune(self, step_w: int = 200, hold_s: float = 2.0) -> None:
        if not self.autotune_enabled:
            return
        now = datetime.now()
        if now - self.last_tune < self.tune_interval or not self._is_quiet():
            return

        prev_op = self.operation
        prev_manual = int(self.manualpower.asNumber)

        try:
            # MANUAL und beruhigen
            await self.update_operation(self.operationmode[0], SmartMode.MANUAL)
            self.manualpower.update_value(0)
            await asyncio.sleep(1.0)

            self._tune_samples.clear(); self._tune_capture = True
            await asyncio.sleep(0.5)

            # Step setzen (Entladen -> Netz sinkt)
            self.manualpower.update_value(step_w)
            t_step = datetime.now()

            await asyncio.sleep(hold_s)
            self.manualpower.update_value(0)
            await asyncio.sleep(0.8)

        finally:
            self._tune_capture = False
            self.last_tune = datetime.now()
            # Ausgangszustand
            self.manualpower.update_value(prev_manual)
            await self.update_operation(self.operationmode[0], prev_op)

        # Auswertung
        if len(self._tune_samples) < 5:
            return
        pre  = [p for (t, p) in self._tune_samples if t <= t_step]
        p0   = sum(pre[-5:]) / max(1, len(pre[-5:]))
        post = [p for (t, p) in self._tune_samples if t >= t_step]
        p_end = sum(post[-10:]) / max(1, len(post[-10:]))

        delta = p_end - p0
        gain  = abs(delta) / max(1, abs(step_w))
        target = p0 + 0.5 * (p_end - p0)
        t50 = None
        for t, p in post:
            if (p0 <= p_end and p >= target) or (p0 > p_end and p <= target):
                t50 = t; break

        if t50:
            self.lead_s = max(0.3, min(2.5, (t50 - t_step).total_seconds()))
        if gain > 0:
            self.plant_gain = max(0.5, min(1.5, gain))

        _LOGGER.info(f"Autotune => lead_s={self.lead_s:.2f}s, gain={self.plant_gain:.2f}, delta={delta:.1f}W @ step={step_w}W")

    async def loadDevices(self) -> None:
        if self.config_entry is None or (data := await Api.Connect(self.hass, dict(self.config_entry.data), True)) is None:
            return
        if (mqtt := data.get("mqtt")) is None:
            return

        # get version number from integration
        integration = await async_get_integration(self.hass, DOMAIN)
        if integration is None:
            _LOGGER.error("Integration not found for domain: %s", DOMAIN)
            return
        self.attr_device_info["sw_version"] = integration.manifest.get("version", "unknown")

        self.operationmode = (
            ZendureRestoreSelect(self, "Operation", {0: "off", 1: "manual", 2: "smart", 3: "smart_discharging", 4: "smart_charging"}, self.update_operation),
        )
        self.manualpower = ZendureRestoreNumber(self, "manual_power", None, None, "W", "power", 10000, -10000, NumberMode.BOX, True)
        self.availableKwh = ZendureSensor(self, "available_kwh", None, "kWh", "energy", None, 1)
        self.power = ZendureSensor(self, "power", None, "W", "power", None, 0)
        self.p1_avg = ZendureSensor(self, "p1_avg", None, "W", "power", None, 0)
        self.p1_stddev = ZendureSensor(self, "p1_stddev", None, "W", "power", None, 0)
        self.p1_powerAverage = ZendureSensor(self, "p1_powerAverage", None, "W", "power", None, 0)
        self.solardischarge = ZendureBinarySensor(self, "solardischarge")
        self.batterydischarge = ZendureBinarySensor(self, "batterydischarge")
        self.batterycharge = ZendureBinarySensor(self, "batterycharge")
        self.isfast = ZendureBinarySensor(self, "isfast")

        # load devices
        for dev in data["deviceList"]:
            try:
                if (deviceId := dev["deviceKey"]) is None or (prodModel := dev["productModel"]) is None:
                    continue
                _LOGGER.info(f"Adding device: {deviceId} {prodModel} => {dev}")

                init = Api.createdevice.get(prodModel.lower(), None)
                if init is None:
                    _LOGGER.info(f"Device {prodModel} is not supported!")
                    continue

                # create the device and mqtt server
                device = init(self.hass, deviceId, prodModel, dev)
                self.devices.append(device)
                Api.devices[deviceId] = device

                if Api.localServer is not None and Api.localServer != "":
                    try:
                        psw = hashlib.md5(deviceId.encode()).hexdigest().upper()[8:24]  # noqa: S324
                        provider: auth_ha.HassAuthProvider = auth_ha.async_get_provider(self.hass)
                        credentials = await provider.async_get_or_create_credentials({"username": deviceId.lower()})
                        user = await self.hass.auth.async_get_user_by_credentials(credentials)
                        if user is None:
                            user = await self.hass.auth.async_create_user(deviceId, group_ids=[GROUP_ID_USER], local_only=False)
                            await provider.async_add_auth(deviceId.lower(), psw)
                            await self.hass.auth.async_link_user(user, credentials)
                        else:
                            await provider.async_change_password(deviceId.lower(), psw)

                        _LOGGER.info(f"Created MQTT user: {deviceId} with password: {psw}")

                    except Exception as err:
                        _LOGGER.error(err)

            except Exception as e:
                _LOGGER.error(f"Unable to create device {e}!")
                _LOGGER.error(traceback.format_exc())

        _LOGGER.info(f"Loaded {len(self.devices)} devices")

        # initialize the api & p1 meter
        await EntityDevice.add_entities()
        self.api.Init(self.config_entry.data, mqtt)
        self.update_p1meter(self.config_entry.data.get(CONF_P1METER, "sensor.power_actual"))
        await asyncio.sleep(1)  # allow other tasks to run
        self.update_fusegroups()

    async def _async_update_data(self) -> None:
        _LOGGER.debug("Updating Zendure data")
        await EntityDevice.add_entities()

        def isBleDevice(device: ZendureDevice, si: bluetooth.BluetoothServiceInfoBleak) -> bool:
            for d in si.manufacturer_data.values():
                try:
                    if d is None or len(d) <= 1:
                        continue
                    sn = d.decode("utf8")[:-1]
                    if device.snNumber.endswith(sn):
                        _LOGGER.info(f"Found Zendure Bluetooth device: {si}")
                        device.attr_device_info["connections"] = {("bluetooth", str(si.address))}
                        return True
                except Exception:  # noqa: S112
                    continue
            return False

        for device in self.devices:
            if isinstance(device, ZendureLegacy) and device.bleMac is None:
                for si in bluetooth.async_discovered_service_info(self.hass, False):
                    if isBleDevice(device, si):
                        break

            _LOGGER.debug(f"Update device: {device.name} ({device.deviceId})")
            await device.dataRefresh(self.update_count)
            device.setStatus()
        self.update_count += 1

        # Manually update the timer
        if self.hass and self.hass.loop.is_running():
            self._schedule_refresh()
        
        asyncio.create_task(self.run_autotune())

    def update_p1meter(self, p1meter: str | None) -> None:
        """Update the P1 meter sensor."""
        _LOGGER.debug("Updating P1 meter to: %s", p1meter)
        if self.p1meterEvent:
            self.p1meterEvent()
        if p1meter:
            self.p1meterEvent = async_track_state_change_event(self.hass, [p1meter], self._p1_changed)
        else:
            self.p1meterEvent = None

    @callback
    async def _p1_changed(self, event: Event[EventStateChangedData]) -> None:
        # update new entities
        await EntityDevice.add_entities()

        # exit if there is nothing to do
        if not self.hass.is_running or not self.hass.is_running or (new_state := event.data["new_state"]) is None:
            return

        try:  # convert the state to a float
            p1 = int(float(new_state.state))
        except ValueError:
            return

        time = datetime.now()

        if self._tune_capture:
            self._tune_samples.append((time, p1))

        # >>> NEU: Puffer & EMA aktualisieren
        self._p1_buf.append((time, p1))
        p1_ema = self._ema(p1)

        # Check for fast delay
        if time < self.zero_fast:
            self.p1_history.append(p1)
            return

        # calculate the standard deviation
        if len(self.p1_history) > 1:
            avg = int(sum(self.p1_history) / len(self.p1_history))
            self.p1_avg.update_value(avg)
            stddev = min(50, sqrt(sum([pow(i - avg, 2) for i in self.p1_history]) / len(self.p1_history)))

            self.p1_stddev_history.append(stddev)

            self.p1_stddev.update_value(stddev)
            self.isfast.update_value(False)
            if isFast := abs(p1 - avg) > SmartMode.Threshold * stddev:
                self.p1_history.clear()
                self.isfast.update_value(True)
        else:
            isFast = False
        self.p1_history.append(p1)

        # check minimal time between updates
        if isFast or time > self.zero_next:
            try:
                self.zero_next = time + timedelta(seconds=SmartMode.TIMEZERO)
                if isFast:
                    self.zero_fast = self.zero_next
                    await self.powerChanged(p1, True)
                else:
                    self.zero_fast = time + timedelta(seconds=SmartMode.TIMEFAST)
                    await self.powerChanged(p1, False)
            except Exception as err:
                _LOGGER.error(err)
                _LOGGER.error(traceback.format_exc())

    async def powerChanged(self, p1: int, isFast: bool) -> None:
        # get the current power
        actualHome = 0
        actualSolar = 0
        availEnergy = 0
        for d in self.devices:
            if await d.power_get():
                actualHome += d.actualHome
                actualSolar += d.actualSolar
                availEnergy += d.availableKwh.asNumber

        # Callback to update power distribution
        async def powerUpdate(power: int, zero: bool = False) -> None:
            _LOGGER.info(f"Update power distribution => {0 if zero else power}W average {powerAverage}W")
            if power == 0 or zero:

                self.solardischarge.update_value(False)
                self.batterydischarge.update_value(False)
                self.batterycharge.update_value(False)

                for d in self.devices:
                    await d.power_discharge(0)
            elif power < -SmartMode.START_POWER:

                self.solardischarge.update_value(False)
                self.batterydischarge.update_value(False)
                self.batterycharge.update_value(True)

                await self.powerCharge(powerAverage, power)
            elif power <= actualSolar:

                self.solardischarge.update_value(True)
                self.batterydischarge.update_value(False)
                self.batterycharge.update_value(False)

                for d in self.devices:
                    d.deivicestate_active.update_value(False)
                    d.deivicestate_inaktive.update_value(False)
                    d.deivicestate_offline.update_value(False)
                    d.deivicestate_starting.update_value(False)
                    d.deivicestate_socfull.update_value(False)

                # excess solar power, only discharge
                _LOGGER.info(f"powerSolar => {power}W average {powerAverage}W")
                for d in sorted(self.devices, key=lambda d: d.actualKwh + d.activeKwh, reverse=True):
                    if d.online and d.state != DeviceState.SOCFULL:
                        d.activeKwh = 0.5 if power > 0 else 0
                        pwr = min(d.actualSolar, max(0, power))
                        power -= await d.power_discharge(pwr)
            else:

                self.solardischarge.update_value(False)
                self.batterydischarge.update_value(True)
                self.batterycharge.update_value(False)

                await self.powerDischarge(powerAverage, power, actualSolar)

        # Update the power entities (Ist-Wert)
        power_raw = actualHome + p1
        self.power.update_value(power_raw)
        self.availableKwh.update_value(availEnergy)
        self.power_history.append(power_raw)

        powerAverage = sum(self.power_history) // len(self.power_history) if len(self.power_history) > 0 else 0
        self.p1_powerAverage.update_value(powerAverage)

        # ===== Look-ahead / Feed-forward =====
        # 1) Prognose des P1 in lead_s Sekunden
        p1_hat, slope = self._predict_p1()
        if p1_hat is None:
            # Fallback: EMA statt Prognose
            p1_hat = self._p1_ema if self._p1_ema is not None else p1

        # 2) Sofortige Gegenstufe bei erkannten Sprüngen
        ff = self._step_feedforward()  # kann 0 sein

        # 3) Ziel: Netz=0 -> wir regeln gegen (Ist+Prognose + FF)
        #    (Vorzeichen-Logik bleibt wie bisher)
        control_power = actualHome + p1_hat + ff

        # 4) Deadband & Rampenbeschränkung
        control_power = self._apply_deadband_and_ramp(control_power)

        # Kleine Qualitätsregel: wenn wir innerhalb der Hysterese sind, wirklich "0" fahren
        if control_power == 0:
            async def powerUpdateWrapper(zero: bool = True):
                return await powerUpdate(0, zero)
        else:
            async def powerUpdateWrapper(zero: bool = False):
                return await powerUpdate(control_power, zero)

        _LOGGER.info(f"P1(pred) => hat:{p1_hat}W, lead_s={self.lead_s:.2f}s, slope:{slope:.1f}W/s, ff:{ff}W, -> ctrl:{control_power}W (raw:{power_raw}W)")

        match self.operation:
            case SmartMode.MATCHING:
                if (powerAverage > 0 and control_power >= 0) or (powerAverage < 0 and control_power <= 0):
                    await powerUpdateWrapper(False)
                else:
                    await powerUpdateWrapper(True)

            case SmartMode.MATCHING_DISCHARGE:
                await powerUpdateWrapper(False)  # wir geben ctrl direkt rein

            case SmartMode.MATCHING_CHARGE:
                await powerUpdateWrapper(False)

            case SmartMode.MANUAL:
                await powerUpdate(int(self.manualpower.asNumber))

    async def powerCharge(self, average: int, power: int) -> None:
        totalKwh = 0.0
        totalMin = 0
        total = average
        starting = average
        count = 0
        _LOGGER.info(f"powerCharge => {power}W average {average}W")

        self.devices = sorted(self.devices, key=lambda d: d.actualKwh + d.activeKwh, reverse=False)

        for d in self.devices:
            start = d.startCharge if d.actualHome == 0 else d.minCharge
            if d.state in (DeviceState.INACTIVE, DeviceState.SOCEMPTY) and (totalMin == 0 or total < start):
                if not d.fuseCharge(d):
                    continue
                if d.actualHome < 0:
                    d.state = DeviceState.ACTIVE
                    d.activeKwh = -SmartMode.KWHSTEP
                    totalKwh += d.actualKwh
                    totalMin += d.minCharge
                    total -= d.startCharge
                    count += 1
                elif totalMin == 0 or starting < start:
                    d.state = DeviceState.STARTING
                    d.activeKwh = -SmartMode.KWHSTEP
                    totalMin += 1
                starting -= d.startCharge

        flexPwr = max(power, power - totalMin)
        for d in self.devices:
            match d.state:
                case DeviceState.ACTIVE:
                    if count == 1:
                        power -= await d.power_charge(min(0, power))
                    else:
                        pwr = max(d.maxCharge - d.minCharge, int(flexPwr * (2 / count - d.actualKwh / totalKwh if totalKwh > 0 else 1)))
                        flexPwr -= pwr
                        totalKwh -= d.actualKwh
                        pwr = d.minCharge + pwr
                        power -= await d.power_charge(min(max(power, pwr), 0))
                        count -= 1
                case DeviceState.STARTING:
                    await d.power_charge(min(0, -SmartMode.STARTWATT - d.actualSolar))
                case DeviceState.OFFLINE:
                    continue
                case _:
                    d.activeKwh = 0
                    await d.power_discharge(d.actualSolar)
        _LOGGER.info(f"powerCharge => left {power}W")

    async def powerDischarge(self, average: int, power: int, solar: int) -> None:
        starting = average - solar
        total = average - solar
        totalMin = 0
        totalWeight = 0.0

        def sortDevices(d: ZendureDevice) -> float:
            d.maxDischarge = max(0, d.limitDischarge - d.actualSolar)
            self.total += d.maxDischarge
            return d.actualKwh + d.activeKwh

        self.total = 0
        self.devices = sorted(self.devices, key=sortDevices, reverse=True)

        _LOGGER.info(f"powerDischarge => {power}W average {average}W, total {self.total}W")
        self.total = 0

        batteryout = 0

        for d in self.devices:
            batteryout += d.batteryOutput.asInt

        for d in self.devices:
            _LOGGER.info(f"state => {d.state} actualhome{d.actualHome} battout {batteryout}")
            start = d.startDischarge if d.batteryOutput.asInt == 0 else d.minDischarge
            if d.state in (DeviceState.INACTIVE, DeviceState.SOCFULL) and ((totalMin == 0 or total > start) or (d.state == DeviceState.SOCFULL and d.actualHome == 0 and batteryout > 0)) and not d.MinSoCWindow:
                _LOGGER.info(f"First Round => {d.name} state: {d.state}")
                if not d.fuseDischarge(d):
                    continue
                if d.batteryOutput.asInt > 0 or (d.state == DeviceState.SOCFULL and d.actualHome > 0):
                    d.state = DeviceState.ACTIVE
                    d.activeKwh = SmartMode.KWHSTEP
                    total -= d.startDischarge
                    totalMin += d.minDischarge
                    totalWeight += d.actualKwh * d.maxDischarge
                    self.total += d.maxDischarge
                elif (totalMin == 0 and starting > SmartMode.START_POWER) or starting > start or (d.state == DeviceState.SOCFULL and d.actualHome == 0 and batteryout > 0):
                    d.state = DeviceState.STARTING
                    d.activeKwh = SmartMode.KWHSTEP
                    totalMin += 1

                starting -= d.startDischarge

        flexPwr = max(0, power - totalMin - solar)

        for d in self.devices:
            _LOGGER.info(f"Second Round => {d.name} state: {d.state}")
            match d.state:
                case DeviceState.ACTIVE:
                    d.deivicestate_active.update_value(True)
                    d.deivicestate_inaktive.update_value(False)
                    d.deivicestate_offline.update_value(False)
                    d.deivicestate_starting.update_value(False)
                    d.deivicestate_socfull.update_value(False)
                    pwr = min(d.maxDischarge - d.minDischarge, int(flexPwr * (d.maxDischarge * d.actualKwh / totalWeight if totalWeight > 0 else 0)))
                    flexPwr -= pwr
                    totalWeight -= d.maxDischarge * d.actualKwh
                    pwr = d.minDischarge + pwr
                    power -= await d.power_discharge(min(power, pwr + d.actualSolar))
                case DeviceState.STARTING:
                    d.deivicestate_active.update_value(False)
                    d.deivicestate_inaktive.update_value(False)
                    d.deivicestate_offline.update_value(False)
                    d.deivicestate_starting.update_value(True)
                    d.deivicestate_socfull.update_value(False)
                    power -= await d.power_discharge(SmartMode.STARTWATT + d.actualSolar) - SmartMode.STARTWATT
                case DeviceState.OFFLINE:
                    d.deivicestate_active.update_value(False)
                    d.deivicestate_inaktive.update_value(False)
                    d.deivicestate_offline.update_value(True)
                    d.deivicestate_starting.update_value(False)
                    d.deivicestate_socfull.update_value(False)
                    continue
                case DeviceState.INACTIVE:
                    d.deivicestate_active.update_value(False)
                    d.deivicestate_inaktive.update_value(True)
                    d.deivicestate_offline.update_value(False)
                    d.deivicestate_starting.update_value(False)
                    d.deivicestate_socfull.update_value(False)
                    d.activeKwh = 0
                    power -= await d.power_discharge(d.actualSolar)
                case DeviceState.SOCFULL:
                    if batteryout > 0 and d.actualHome > 0:
                        d.deivicestate_active.update_value(False)
                        d.deivicestate_inaktive.update_value(False)
                        d.deivicestate_offline.update_value(False)
                        d.deivicestate_starting.update_value(False)
                        d.deivicestate_socfull.update_value(True)
                        d.activeKwh = 0
                        power -= await d.power_discharge(d.actualSolar)
                
        _LOGGER.info(f"powerDischarge => left {power}W")

    def update_fusegroups(self) -> None:
        _LOGGER.info("Update fusegroups")

        # updateFuseGroup callback
        def updateFuseGroup(_entity: ZendureRestoreSelect, _value: Any) -> None:
            self.update_fusegroups()

        fuseGroups: dict[str, FuseGroup] = {}
        for device in self.devices:
            try:
                if device.fuseGroup.onchanged is None:
                    device.fuseGroup.onchanged = updateFuseGroup

                match device.fuseGroup.state:
                    case "owncircuit" | "group3600":
                        fg = FuseGroup(device.name, 3600, -3600)
                    case "group800":
                        fg = FuseGroup(device.name, 800, -1200)
                    case "group1200":
                        fg = FuseGroup(device.name, 1200, -1200)
                    case "group2000":
                        fg = FuseGroup(device.name, 2000, -2000)
                    case "group2400":
                        fg = FuseGroup(device.name, 2400, -2400)
                    case _:
                        continue

                fg.devices.append(device)
                fuseGroups[device.deviceId] = fg
            except:  # noqa: E722
                _LOGGER.error(f"Unable to create fusegroup for device: {device.name} ({device.deviceId})")

        # Update the fusegroups and select optins for each device
        for device in self.devices:
            try:
                fusegroups: dict[Any, str] = {
                    0: "unused",
                    1: "owncircuit",
                    2: "group800",
                    3: "group1200",
                    4: "group2000",
                    5: "group2400",
                    6: "group3600",
                }
                for deviceId, fg in fuseGroups.items():
                    if deviceId != device.deviceId:
                        fusegroups[deviceId] = f"Part of {fg.name} fusegroup"
                device.fuseGroup.setDict(fusegroups)
            except:  # noqa: E722
                _LOGGER.error(f"Unable to create fusegroup for device: {device.name} ({device.deviceId})")

        # Add devices to fusegroups
        for device in self.devices:
            if fg := fuseGroups.get(device.fuseGroup.value):
                fg.devices.append(device)
            device.setStatus()

        # check if we can split fuse groups
        self.fuseGroups.clear()
        for fg in fuseGroups.values():
            if len(fg.devices) > 1 and fg.maxpower >= sum(d.limitDischarge for d in fg.devices) and fg.minpower <= sum(d.limitCharge for d in fg.devices):
                for d in fg.devices:
                    self.fuseGroups.append(FuseGroup(d.name, d.limitDischarge, d.limitCharge, [d]))
            else:
                for d in fg.devices:
                    d.fuseCharge = fg.fuseCharge
                    d.fuseDischarge = fg.fuseDischarge
                self.fuseGroups.append(fg)

    async def update_operation(self, entity: ZendureSelect, _operation: Any) -> None:
        operation = int(entity.value)
        _LOGGER.info(f"Update operation: {operation} from: {self.operation}")

        self.operation = operation
        self.power_history.clear()
        if self.p1meterEvent is not None:
            if operation != SmartMode.NONE and (len(self.devices) == 0 or all(not d.online for d in self.devices)):
                _LOGGER.warning("No devices online, not possible to start the operation")
                persistent_notification.async_create(self.hass, "No devices online, not possible to start the operation", "Zendure", "zendure_ha")
                return

            match self.operation:
                case SmartMode.NONE:
                    self.solardischarge.update_value(False)
                    self.batterydischarge.update_value(False)
                    for d in self.devices:
                        d.deivicestate_active.update_value(False)
                        d.deivicestate_inaktive.update_value(False)
                        d.deivicestate_offline.update_value(False)
                        d.deivicestate_starting.update_value(False)
                    d.deivicestate_socfull.update_value(False)
                    if len(self.devices) > 0:
                        for d in self.devices:
                            await d.power_off()
