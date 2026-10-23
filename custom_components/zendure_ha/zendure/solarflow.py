"""ZendureSolarFlow: Zendure battery device with MQTT/HTTP/BLE control."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import paho.mqtt.client as mqtt
from aiohttp import ClientTimeout
from homeassistant.components.number import NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..binary_sensor import HaBinarySensor
from ..const import PhaseModi
from ..device import HOLD_S, REPORT_WAIT_S, DeviceState, DistributionDevice, ProfileCharge, ProfileDischarge
from ..entity import HaEntity
from ..number import HaNumber
from ..select import HaSelect
from ..sensor import HaSensor
from ..switch import HaSwitch
from .battery import ZendureBattery
from .device import Command, ConnectionType, ZendureDevice, centi_convert, temp_convert

if TYPE_CHECKING:
    from .driver import ZendureDriver


_LOGGER = logging.getLogger(__name__)

HTTP_HEADER = {"content-type": "application/json; charset=UTF-8"}
HTTP_TIMEOUT = ClientTimeout(total=1)
OFFLINE_TIMEOUT = 2400.0  # some devices report in bursts with more than a minute of silence in between
POWER_SET = 40
POWER_MAX_TIME = 10



def _limits(watts: int) -> dict:
    # the zenSDK/BLE setpoint: the limits written straight to the device; acMode 1 charges, 2 discharges
    if watts == 0:
        return {"properties": {"smartMode": 0, "acMode": 2, "outputLimit": 0, "inputLimit": 0}}
    if watts < 0:
        return {"properties": {"smartMode": 1, "acMode": 1, "outputLimit": 0, "inputLimit": -watts}}
    return {"properties": {"smartMode": 1, "acMode": 2, "outputLimit": watts, "inputLimit": 0}}


class ZendureSolarFlow(ZendureDevice, DistributionDevice):
    """Zendure battery device with MQTT/HTTP/BLE communication."""

    def __init__(self, hass: HomeAssistant, driver: ZendureDriver | None, subentry_id: str | None, name: str, data: dict[str, Any], model: str, pwr_max: int, pwr_min: int, pwr_solar: int) -> None:
        ZendureDevice.__init__(self, hass, driver, subentry_id, name, data, model)
        DistributionDevice.__init__(self, hass, data["sn"], [ProfileDischarge(pwr_max), ProfileCharge(pwr_min)], name, subentry_id)
        self.connection = ConnectionType.OFFLINE
        self.entities["output_limit"].update_range(0, abs(pwr_max))
        self.entities["input_limit"].update_range(0, abs(pwr_min))
        self._msg_update = 0.0
        self.mqtt: mqtt.Client | None = None
        self.batteries: dict[str, ZendureBattery] = {}
        self.battery_names: dict[str, str] = dict(data.get("batteries", {}))  # pack serial -> name, fixed at first sight like the device name
        self.home_input: int = 0
        self.home_output: int = 0
        self.input_limit: int = 0  # the limits the device reports it works with: the echo of our last command
        self.output_limit: int = 0
        self.device_queue: asyncio.Queue[tuple[Command, Any]] = asyncio.Queue()
        self.device_task = asyncio.get_running_loop().create_task(self.device_loop())

    def unload(self) -> None:
        if self.device_task is not None:
            self.device_task.cancel()

    def subentry_data(self) -> dict[str, Any]:
        return {**ZendureDevice.subentry_data(self), "batteries": self.battery_names}

    # only created on the first report: which of these a device sends depends on its model and its wiring
    SENSORS: dict[str, tuple[type[HaEntity], str, dict[str, Any]]] = {
        "acFirmwareVersion": (HaSensor, "ac_firmware_version", {"disabled": True}),
        "acHardwareVersion": (HaSensor, "ac_hardware_version", {"disabled": True}),
        "acOutputPower": (HaSensor, "ac_output_power", {"uom": "W", "device_class": "power", "state_class": "measurement"}),
        "autoHeat": (HaSelect, "auto_heat", {"options": {0: "off", 1: "on"}}),
        "autoRecover": (HaSwitch, "auto_recover", {"disabled": True}),
        "batteryElectric": (HaSensor, "battery_electric", {"uom": "W", "device_class": "power", "state_class": "measurement"}),
        "BatVolt": (HaSensor, "bat_volt", {"uom": "V", "device_class": "voltage", "state_class": "measurement", "convert": centi_convert}),
        "bmsFirmwareVersion": (HaSensor, "bms_firmware_version", {"disabled": True}),
        "bmsHardwareVersion": (HaSensor, "bms_hardware_version", {"disabled": True}),
        "buzzerSwitch": (HaSwitch, "buzzer_switch", {"disabled": True}),
        "ctOff": (HaBinarySensor, "ct_off", {"disabled": True}),
        "dcFirmwareVersion": (HaSensor, "dc_firmware_version", {"disabled": True}),
        "dcHardwareVersion": (HaSensor, "dc_hardware_version", {"disabled": True}),
        "dcOutputPower": (HaSensor, "dc_output_power", {"uom": "W", "device_class": "power", "state_class": "measurement"}),
        "dspversion": (HaSensor, "dsp_version", {"disabled": True}),
        "energyPower": (HaSensor, "energy_power", {"uom": "W", "state_class": "measurement", "disabled": True}),
        "Fanmode": (HaSwitch, "fanmode", {}),
        "fanSpeed": (HaSelect, "fan_speed", {"options": {0: "auto", 1: "normal", 2: "fast"}}),
        "Fanspeed": (HaSelect, "fan_speed", {"options": {0: "auto", 1: "normal", 2: "fast"}}),
        "fanSwitch": (HaSwitch, "fan_switch", {}),
        "gridOffMode": (HaSelect, "grid_off_mode", {"options": {0: "normal", 1: "eco", 2: "off"}}),
        "gridOffPower": (HaSensor, "grid_off_power", {"uom": "W", "device_class": "power", "state_class": "measurement", "aggr": True}),
        "gridPower": (HaSensor, "grid_power", {"uom": "W", "device_class": "power", "state_class": "measurement", "aggr": True}),
        "gridReverse": (HaSelect, "grid_reverse", {"options": {0: "disabled", 1: "allow", 2: "forbidden"}}),
        "inverseMaxPower": (HaSensor, "inverse_max_power", {"uom": "W", "disabled": True}),
        "lampSwitch": (HaSwitch, "lamp_switch", {}),
        "localState": (HaBinarySensor, "local_state", {"disabled": True}),
        "lowTemperature": (HaBinarySensor, "low_temperature", {}),
        "masterFirmwareVersion": (HaSensor, "master_firmware_version", {"disabled": True}),
        "masterhaerVersion": (HaSensor, "masterhaer_version", {"disabled": True}),
        "masterHardwareVersion": (HaSensor, "master_hardware_version", {"disabled": True}),
        "masterSoftVersion": (HaSensor, "master_soft_version", {"disabled": True}),
        "masterSwitch": (HaBinarySensor, "master_switch", {"disabled": True}),
        "mpptFirmwareVersion": (HaSensor, "mppt_firmware_version", {"disabled": True}),
        "restState": (HaBinarySensor, "rest_state", {"disabled": True}),
        "reverseState": (HaBinarySensor, "reverse_state", {}),
        "rssi": (HaSensor, "rssi", {"uom": "dBm", "device_class": "signal_strength", "state_class": "measurement", "disabled": True}),
        "softVersion": (HaSensor, "soft_version", {"disabled": True}),
        "soh": (HaSensor, "soh", {"uom": "%", "state_class": "measurement", "convert": lambda v: v / 10.0}),
        "solarInputPower": (HaSensor, "solar", {"uom": "W", "device_class": "power", "state_class": "measurement", "icon": "mdi:solar-panel", "aggr": True}),
        "solarPower1": (HaSensor, "solar_power1", {"uom": "W", "device_class": "power", "state_class": "measurement", "icon": "mdi:solar-panel"}),
        "solarPower2": (HaSensor, "solar_power2", {"uom": "W", "device_class": "power", "state_class": "measurement", "icon": "mdi:solar-panel"}),
        "solarPower3": (HaSensor, "solar_power3", {"uom": "W", "device_class": "power", "state_class": "measurement", "icon": "mdi:solar-panel"}),
        "solarPower4": (HaSensor, "solar_power4", {"uom": "W", "device_class": "power", "state_class": "measurement", "icon": "mdi:solar-panel"}),
        "solarPower5": (HaSensor, "solar_power5", {"uom": "W", "device_class": "power", "state_class": "measurement", "icon": "mdi:solar-panel"}),
        "solarPower6": (HaSensor, "solar_power6", {"uom": "W", "device_class": "power", "state_class": "measurement", "icon": "mdi:solar-panel"}),
        "totalBatteryVolt": (HaSensor, "total_battery_volt", {"uom": "V", "device_class": "voltage", "state_class": "measurement", "convert": centi_convert}),
        "VoltWakeup": (HaSensor, "volt_wakeup", {"uom": "V", "device_class": "voltage", "state_class": "measurement", "disabled": True}),
        "wifiState": (HaBinarySensor, "wifi_state", {"disabled": True}),
    }

    def entity_create(self) -> None:
        HaSensor(self, "soc", "electricLevel", uom="%", device_class="battery", state_class="measurement")
        HaNumber(self, "soc_min", "minSoc", uom="%", minimum=0, maximum=100, factor=0.1, restore=True)
        HaNumber(self, "soc_max", "socSet", uom="%", minimum=0, maximum=100, factor=0.1, restore=True)
        HaSensor(self, "soc_limit", "socLimit", state_class="measurement")
        # the device's own limits, for automations while the distribution is off; the range follows the app limits below
        HaNumber(self, "output_limit", "outputLimit", uom="W", minimum=0, maximum=800, mode=NumberMode.SLIDER)
        HaNumber(self, "input_limit", "inputLimit", uom="W", minimum=0, maximum=800, mode=NumberMode.SLIDER)

        HaSensor(self, "home_input", "gridInputPower", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "home_output", "outputHomePower", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "battery_input", "outputPackPower", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "battery_output", "packInputPower", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "temp", "hyperTmp", uom="°C", device_class="temperature", state_class="measurement", convert=temp_convert)
        HaSensor(self, "remaining_time", "remainingTime", uom="h", device_class="duration", state_class="measurement")
        HaSensor(self, "power_max", "inverseMaxPower", uom="W", device_class="power", state_class="measurement")
        self.setpoint = HaSensor(self, "power_setpoint", uom="W", device_class="power", state_class="measurement")
        HaSelect(self, "power_phase", PhaseModi, on_changed=self.update_phase, restore=True)

        HaBinarySensor(self, "bypass", "pass")
        HaBinarySensor(self, "heat_state", "heatState")
        HaBinarySensor(self, "hems_state", "hemsState")

        self.kwhsensor = HaSensor(self, "kwh", uom="kWh", device_class="energy")
        HaSensor(self, "rte", uom="%", state_class="measurement", precision=1)

    def mqtt_publish(self, topic: str, command: dict) -> None:
        """Send a JSON command to the device via MQTT."""
        if not self.mqtt or not self.device_key:
            return
        self._msg_id += 1
        command["messageId"] = self._msg_id
        command["timestamp"] = int(time.time())
        command["deviceId"] = self.device_key
        topic = f"iot/{self.prodkey}/{self.device_key}/{topic}"
        payload = json.dumps(command, default=lambda o: o.__dict__)
        self.mqtt.publish(topic, payload)

    def mqtt_power(self, watts: int) -> None:
        """Broker command for a setpoint: the limits zenSDK writes, as a property write."""
        self.mqtt_publish("properties/write", _limits(watts))

    def mqtt_power_off(self) -> None:
        self.mqtt_publish("properties/write", _limits(0))

    def entity_write(self, entity, value):
        command = {"properties": {entity.vendor_id: value}}
        if self.connection in [ConnectionType.CLOUD, ConnectionType.LOCAL]:
            self.mqtt_publish("properties/write", command)
        elif self.connection != ConnectionType.OFFLINE:
            self.device_queue.put_nowait((Command.WRITE, command))

    def power(self, watts: int) -> int:
        watts = max(self.power_profile[1].power, min(self.power_profile[0].power, watts))  # clamp to rated power, both directions

        # outputPower is an absolute target and the pass-through is already flowing, so ask for it on top or the command sits below it
        if self.bypass and watts > 0:
            watts = min(self.power_profile[0].power, watts + self.solar)

        # the device is busy with its last command: over ZenSDK until the report echoes the limits (REPORT_WAIT_S at most), over the broker HOLD_S
        since = time.time() - self.power_since
        if self.connection == ConnectionType.ZENSDK:
            if (self.input_limit, self.output_limit) != ((expected := _limits(self.power_setpoint)["properties"])["inputLimit"], expected["outputLimit"]) and since < REPORT_WAIT_S:
                return self.power_actual
        elif since < HOLD_S:
            return self.power_actual

        # no new command while both the measured power and the last setpoint sit within 10 W of the new target
        if abs(self.power_actual - watts) < 10 and abs(self.power_setpoint - watts) < 10:
            return self.power_actual
        if self.power_setpoint != watts:
            self.power_since = time.time()
        self.setpoint.update_value(watts)  # publish the setpoint we are about to send, so the recorder holds the command side too
        if self.connection in [ConnectionType.CLOUD, ConnectionType.LOCAL]:
            self.mqtt_power(watts)
        elif self.connection != ConnectionType.OFFLINE:
            self.device_queue.put_nowait((Command.POWER, _limits(watts)))
        else:
            return self.power_actual
        self.set_state(DeviceState.SENT)  # the state machine follows our setpoint only: SENT, CONFIRM on the reply, ACTIVE once the power follows
        return self.power_actual

    def power_off(self) -> None:
        # hems overrules the settings while its flag is up: 0 W first, then the flag down on its own write
        if self.connection in [ConnectionType.CLOUD, ConnectionType.LOCAL]:
            self.mqtt_power_off()
            self.mqtt_publish("properties/write", {"properties": {"hemsState": 0}})
        elif self.connection != ConnectionType.OFFLINE:
            self.device_queue.put_nowait((Command.POWER, _limits(0)))
            self.device_queue.put_nowait((Command.WRITE, {"properties": {"hemsState": 0}}))
        self.setpoint.update_value(0)
        self.set_state(DeviceState.EXTERNAL)

    def update_properties(self, payload: dict, now: float, on_ha: bool = False) -> None:
        # update last update
        self._msg_update = now

        # update the batteries
        if batprops := payload.get("packData", None):
            for bp in batprops:
                if (sn := bp.pop("sn", None)) is not None:
                    if (bat := self.batteries.get(sn, None)) is None:
                        if not on_ha:
                            bp["sn"] = sn
                            self.hass.loop.call_soon_threadsafe(self.update_properties, payload, now, True)
                            return
                        bat = ZendureBattery._registry.get(sn) or ZendureBattery(self.hass, sn, self, bp.get("packType"))
                        self.batteries[sn] = bat
                        if (kwh := sum(0 if b is None else b.kWh for b in self.batteries.values())) != self.kwh:
                            self.kwhsensor.update_value(kwh)
                            self.on_invalidate()
                    if bat and bp:
                        bat.update_properties(bp, now)

        # update the device entities
        for k, v in (payload.get("properties") or {}).items():
            # update the entities; the state handling below must also run on unchanged values
            self.entity_update(k, v, on_ha)

            if k in ["outputHomePower", "gridInputPower"]:
                self.power_actual = self.home_output - self.home_input
                if abs(self.power_actual - self.power_setpoint) < POWER_SET or (now - self.state_since) > POWER_MAX_TIME:
                    self.set_state(DeviceState.ACTIVE)
            elif k == "inverseMaxPower":
                self.power_profile[0].power = v  # the discharge limit as configured in the Zendure app
                self.power_profile[0].__post_init__()  # floor and bands follow the new limit
                self.entities["output_limit"].update_range(0, v)
            elif k in ["chargeLimit", "chargeMaxLimit"]:
                self.entities["input_limit"].update_range(0, v)
            elif k in ["minSoc", "socSet", "socLimit"]:
                # reorder the devices based on capacity
                if self.on_invalidate:
                    self.on_invalidate()
        if not self.ip and (ip := payload.get("ip")):
            self.ip = ip
            self.config_changed()

    async def device_loop(self) -> None:
        timeout = 2
        while True:
            try:
                # get next item from queue
                try:
                    async with asyncio.timeout(timeout):
                        cmd, value = await self.device_queue.get()
                except asyncio.TimeoutError:
                    cmd, value = Command.TIMEOUT, None

                # first the state, every round, whatever the command was
                if self._msg_update + OFFLINE_TIMEOUT < time.time():
                    self.connection = ConnectionType.OFFLINE
                    self.power_actual = 0
                    self.set_state(DeviceState.OFFLINE)

                # then act on that state
                match self.connection:
                    case ConnectionType.OFFLINE:
                        await self.cmd_recover()
                        timeout = 2 if self.connection != ConnectionType.OFFLINE else min(300, 2 * timeout)
                    case ConnectionType.ZENSDK:
                        timeout = 2
                        await self.cmd_zensdk(value)
                    case ConnectionType.BLE:
                        timeout = 2
                        if cmd != Command.TIMEOUT:
                            await self.cmd_ble(value)
                    case _:
                        # on the broker: a device with a known ip moves to zenSDK as soon as its local API answers
                        timeout = 5
                        if self.ip:
                            await self.cmd_zensdk(None)

            except asyncio.CancelledError:
                raise
            except Exception:
                # the task must never die
                _LOGGER.exception("device task %s", self.device_id)

    async def cmd_zensdk(self, payload: Any) -> None:
        try:
            session = async_get_clientsession(self.hass)

            # do the command; the local API rejects a body without the serial and a message id
            if payload:
                self._msg_id += 1
                body = json.dumps({**payload, "sn": self.sn, "id": self._msg_id})
                response = await session.post(f"http://{self.ip}/properties/write", data=body, headers=HTTP_HEADER, timeout=HTTP_TIMEOUT)
                if response.status >= 400:
                    _LOGGER.warning("ZENSDK write rejected for %s: HTTP %s %s", self.device_id, response.status, (await response.text())[:200])

            # get the properties; an answer puts the device on zenSDK, also one that came in over the broker
            response = await session.get(f"http://{self.ip}/properties/report", headers=HTTP_HEADER, timeout=HTTP_TIMEOUT)
            payload = json.loads(await response.text())
            self.update_properties(payload, time.time(), True)
            self.connection = ConnectionType.ZENSDK

        except Exception:
            # no answer while on zenSDK: offline, so the next round recovers it over HTTP, BLE or the broker
            if self.connection == ConnectionType.ZENSDK:
                _LOGGER.warning("ZENSDK failed for %s", self.device_id)
                self.connection = ConnectionType.OFFLINE

    async def cmd_ble(self, payload: Any) -> None:
        if not await self._ble_connect():
            return
        if payload:
            await self._ble_command(payload, "write")

    async def cmd_recover(self) -> None:
        if self.ip:
            try:
                session = async_get_clientsession(self.hass)
                async with session.get(f"http://{self.ip}/properties/report", headers=HTTP_HEADER, timeout=HTTP_TIMEOUT) as response:
                    self.update_properties(json.loads(await response.text()), time.time(), True)
                self.connection = ConnectionType.ZENSDK
                return
            except Exception:
                pass  # no luck connection

        if self.ble_name and await self._ble_connect():
            self.connection = ConnectionType.BLE


class ZendureLegacy(ZendureSolarFlow):
    """Models without hemsEP (AIO 2400, Ace 1500): the broker command is deviceAutomation with a nested autoModelValue."""

    def automation_value(self, watts: int) -> Any:
        return {"chargingType": 1 if watts < 0 else 0, "chargingPower": max(0, -watts), "freq": 0, "outPower": max(0, watts)}

    def mqtt_power(self, watts: int) -> None:
        self.mqtt_publish("function/invoke", {"function": "deviceAutomation", "deviceKey": self.device_key, "arguments": [{"autoModelProgram": 2, "autoModelValue": self.automation_value(watts), "msgType": 1, "autoModel": 8}]})

    def mqtt_power_off(self) -> None:
        self.mqtt_publish("function/invoke", {"function": "deviceAutomation", "deviceKey": self.device_key, "arguments": [{"autoModelProgram": 0, "autoModelValue": self.automation_value(0), "msgType": 1, "autoModel": 0}]})


class ZendureHub(ZendureLegacy):
    """Hub 1200 / Hub 2000: deviceAutomation with a bare number; the Hub cannot charge from AC, so its charge profile is 0."""

    # open: with an ACE 1500 attached the AC input exists, but the app sets inputLimit on the ACE, not here (#1508)
    def automation_value(self, watts: int) -> Any:
        return max(0, watts)


class ZendureHyper(ZendureSolarFlow):
    """Hyper 2000: steered over hemsEP, which the device applies from RAM. Not deviceAutomation: its charge path writes the Hyper flash on every power change."""

    _hems_active = False  # hemsState is raised once; power_off lowers it again

    def mqtt_power(self, watts: int) -> None:
        # hemsEP behind hemsState=1; charge carries chargePower, discharge outputPower, freq 0 applies at once
        if not self._hems_active:
            self.mqtt_publish("properties/write", {"properties": {"hemsState": 1}})
            self._hems_active = True
        arguments = {"outputPower": 0, "chargePower": -watts, "freq": 0, "mode": 9, "chargeMode": 3} if watts < 0 else {"outputPower": watts, "chargePower": 0, "freq": 0, "minSoc": 100, "mode": 9}
        self.mqtt_publish("function/invoke", {"function": "hemsEP", "arguments": arguments})

    def mqtt_power_off(self) -> None:
        # 0 W over hemsEP; the base power_off then lowers hemsState
        self.mqtt_publish("function/invoke", {"function": "hemsEP", "arguments": {"outputPower": 0, "chargePower": 0, "freq": 0, "mode": 9, "chargeMode": 3}})
        self._hems_active = False
