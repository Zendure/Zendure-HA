"""Coordinator for Zendure integration."""

import json
import logging
import traceback
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.number import NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import Api
from .const import ManagerMode
from .device import ZendureDevice
from .devices.ace1500 import ACE1500
from .devices.aio2400 import AIO2400
from .devices.hub1200 import Hub1200
from .devices.hub2000 import Hub2000
from .devices.hyper2000 import Hyper2000
from .devices.solarflow800 import SolarFlow800
from .devices.solarflow800Plus import SolarFlow800Plus
from .devices.solarflow800Pro import SolarFlow800Pro
from .devices.solarflow2400ac import SolarFlow2400AC
from .devices.superbasev4600 import SuperBaseV4600
from .devices.superbasev6400 import SuperBaseV6400
from .distribution import Distribution
from .entity import ZendureEntities
from .fusegroup import FuseGroup
from .number import ZendureRestoreNumber
from .select import ZendureRestoreSelect
from .sensor import ZendureSensor
from .smartmeter import ZendureSmartMeter

_LOGGER = logging.getLogger(__name__)

type ZendureConfigEntry = ConfigEntry[ZendureCoordinator]

CONST_TOPIC_CNT = 4


class ZendureCoordinator(DataUpdateCoordinator[None], ZendureEntities):
    """Zendure coordinator."""

    models: dict[str, tuple[str, type[ZendureEntities]]] = {
        "a4ss5p": ("SolarFlow 800", SolarFlow800),
        "b1nhmc": ("SolarFlow 800", SolarFlow800),
        "n8sky9": ("SolarFlow 800AC", SolarFlow800),
        "8n77v3": ("SolarFlow 800Plus", SolarFlow800Plus),
        "r3mn8u": ("SolarFlow 800Pro", SolarFlow800Pro),
        "bc8b7f": ("SolarFlow 2400AC", SolarFlow2400AC),
        "2qe7c9": ("SolarFlow 2400Pro", SolarFlow2400AC),
        "5fg27j": ("SolarFlow 2400AC+", SolarFlow2400AC),
        "c3yt68": ("smartMeter 3CT", ZendureSmartMeter),
        "y6hvtw": ("smartMeter 3CT-S", ZendureSmartMeter),
        "1dmcr8": ("smartPlug", ZendureSmartMeter),
        "vv1wd7": ("smartCt", ZendureSmartMeter),
        "gda3tb": ("Hyper 2000", Hyper2000),
        "b3dxda": ("Hyper 2000", Hyper2000),
        "ja72u0": ("Hyper 2000", Hyper2000),
        "73bktv": ("Hub 1200", Hub1200),
        "a8yh63": ("Hub 2000", Hub2000),
        "ywf7hv": ("AIO 2400", AIO2400),
        "8bm93h": ("ACE 1500", ACE1500),
    }

    def __init__(self, hass: HomeAssistant, entry: ZendureConfigEntry) -> None:
        """Initialize Zendure Coordinator."""
        super().__init__(hass, _LOGGER, name="Zendure Coordinator", update_interval=timedelta(seconds=30), config_entry=entry)
        ZendureEntities.__init__(self, self.hass, "Zendure Coordinator")

        self.operation: ManagerMode = ManagerMode.OFF
        self.power = ZendureSensor(self, "power", None, "W", "power", "measurement", 0)
        self.distribution = Distribution(self.hass, entry.data.get("p1meter", ""), self.power)
        self.operationmode = ZendureRestoreSelect(self, "Operation", {0: "off", 1: "manual", 2: "smart", 3: "smart_discharging", 4: "smart_charging"}, self.update_operation)
        self.operationstate = ZendureSensor(self, "operation_state")
        self.bypassmode = ZendureRestoreSelect(self, "Bypass", {0: "never", 1: "house", 2: "grid"}, self.update_bypass)
        self.manualpower = ZendureRestoreNumber(self, "manual_power", self.update_manualpower, None, "W", "power", 12000, -12000, NumberMode.BOX, True)
        self.availableKwh = ZendureSensor(self, "available_kwh", None, "kWh", "energy", None, 1)
        self.devices: dict[str, ZendureEntities] = {}
        self.fuseGroups: list[FuseGroup] = []

    async def _async_setup(self) -> None:
        """Set up the coordinator."""
        _LOGGER.debug("Setting up Zendure coordinator for entry: %s", self.config_entry.entry_id)
        try:
            device_registry = dr.async_get(self.hass)
            for d in dr.async_entries_for_config_entry(device_registry, self.config_entry.entry_id):
                if d.serial_number and d.model_id and d.hw_version:
                    model_key = d.model_id.lower()
                    if prod := self.models.get(model_key):
                        sn = d.serial_number
                        deviceId = d.hw_version
                        device = prod[1](self.hass, deviceId, sn, prod[0], d.model_id)
                        self.devices[deviceId] = device
                        if isinstance(device, ZendureDevice):
                            self.distribution.devices.append(device)
        except Exception:
            _LOGGER.exception("Unexpected error in MQTT message handler")
        await self.update_fusegroups()
        await self.async_update(self.hass, self.config_entry)
        await mqtt.async_subscribe(self.hass, "/#", self.mqtt_message_received, 1)

        info = self.hass.config_entries.async_loaded_entries(mqtt.DOMAIN)
        if info is not None and len(info) > 0 and (data := info[0].data) is not None:
            Api.iotUrl = self.hass.config.api.local_ip if "core-mosquitto" in data["broker"].lower() else data["broker"]

    async def async_update(self, _hass: HomeAssistant, entry: ZendureConfigEntry) -> None:
        """Handle options update."""
        _LOGGER.debug("async_update: %s", entry.entry_id)
        Api.wifi_ssid = entry.data.get("wifissid", "")
        Api.wifi_psw = entry.data.get("wifipsw", "")
        # Api.iotUrl = mqtt.
        #     _LOGGER.debug("Updating Zendure config entry: %s", entry.entry_id)
        #     Api.mqttLogging = entry.data.get(CONF_MQTTLOG, False)
        #     ZendureManager.simulation = entry.data.get(CONF_SIM, False)
        #     entry.runtime_data.update_p1meter(entry.data.get(CONF_P1METER, "sensor.power_actual"))

    async def async_unload(self) -> None:
        """Unload the coordinator."""
        _LOGGER.debug("async_unload: %s", self.config_entry.entry_id)

    async def _async_update_data(self) -> None:
        _LOGGER.debug("Updating Zendure coordinator data for entry")
        for device in self.devices.values():
            device.refresh()

        # Manually update the timer
        if self.hass and self.hass.loop.is_running():
            self._schedule_refresh()

    async def update_operation(self, entity: ZendureRestoreSelect, _operation: Any) -> None:
        operation = ManagerMode(entity.value)
        self.distribution.operation = operation
        _LOGGER.info(f"Update operation: {operation} from: {self.operation}")

        # self.operation = operation
        # if self.p1meterEvent is not None:
        #     if operation != ManagerMode.OFF and (len(self.devices) == 0 or all(not d.online for d in self.devices)):
        #         _LOGGER.warning("No devices online, not possible to start the operation")
        #         persistent_notification.async_create(self.hass, "No devices online, not possible to start the operation", "Zendure", "zendure_ha")
        #         return

        #     match self.operation:
        #         case ManagerMode.OFF:
        #             if len(self.devices) > 0:
        #                 for d in self.devices:
        #                     await d.power_off()

    async def update_bypass(self, _entity: ZendureRestoreSelect, _operation: Any) -> None:
        _LOGGER.info("Update bypass")

    async def update_manualpower(self, _entity: Any, power: Any) -> None:
        self.distribution.manualpower = power

    async def update_fusegroups(self) -> None:
        _LOGGER.info("Update fusegroups")

        # updateFuseGroup callback
        async def updateFuseGroup(_entity: ZendureRestoreSelect, _value: Any) -> None:
            await self.update_fusegroups()

        fuseGroups: dict[str, FuseGroup] = {}
        for device in self.devices.values():
            await device.setFuseGroup(updateFuseGroup)
            if device.fuseGrp is not None:
                device.fuseGrp.devices.append(device)
                fuseGroups[device.deviceId] = device.fuseGrp

        # Update the fusegroups and select optins for each device
        for device in self.devices.values():
            try:
                fusegroups = ZendureDevice.fuseGroups.copy()
                for deviceId, fg in fuseGroups.items():
                    if deviceId != device.deviceId:
                        fusegroups[deviceId] = f"Part of {fg.name} fusegroup"
                device.fuseGroup.setDict(fusegroups)
            except AttributeError as err:
                _LOGGER.error("Device %s missing fuseGroup attribute: %s", device.name, err)
            except Exception as err:
                _LOGGER.error("Unable to update fusegroup options for device %s (%s): %s", device.name, device.deviceId, err, exc_info=True)

        # Add devices to fusegroups
        for device in self.devices.values():
            if fg := fuseGroups.get(device.fuseGroup.value):
                device.fuseGrp = fg
                fg.devices.append(device)
            device.setStatus()

        # check if we can split fuse groups
        self.fuseGroups.clear()
        for fg in fuseGroups.values():
            if len(fg.devices) > 1 and fg.maxpower >= sum(d.limit[1] for d in fg.devices) and fg.minpower <= sum(d.limit[0] for d in fg.devices):
                for d in fg.devices:
                    self.fuseGroups.append(FuseGroup(d.name, d.limit[1], d.limit[0], [d]))
            else:
                for d in fg.devices:
                    d.fuseGrp = fg
                self.fuseGroups.append(fg)

    @callback
    def mqtt_message_received(self, msg: mqtt.ReceiveMessage) -> None:
        """Handle Zendure mqtt messages."""
        if msg.payload is None or not msg.payload:
            return
        try:
            # Validate topic format before accessing indices
            if len(topics := msg.topic.split("/", 3)) < CONST_TOPIC_CNT:
                _LOGGER.warning("Invalid MQTT topic format: %s (expected 4 segments)", msg.topic)
                return

            # deserialize payload
            deviceId = topics[2]
            try:
                payload = json.loads(msg.payload)
            except json.JSONDecodeError as err:
                _LOGGER.error("Failed to decode JSON from device %s: %s", deviceId, err)
                return
            except UnicodeDecodeError as err:
                _LOGGER.error("Failed to decode payload encoding from device %s: %s", deviceId, err)
                return

            if (device := self.devices.get(deviceId, None)) is not None:
                device.setStatus(datetime.now())
                match topics[3]:
                    case "properties/report":
                        device.entityRead(payload)
                    case "register":
                        device.mqttRegister(payload)
                    case "function/invoke/reply" | "properties/write/reply":
                        device.ready = datetime.min
                    case _:
                        pass

                # if self.mqttLogging:
                # _LOGGER.info("Topic: %s => %s", msg.topic.replace(device.deviceId, device.name).replace(device.snNumber, "snxxx"), payload)
            elif (lg := payload.get("log", None)) is not None and (sn := lg.get("sn", None)) is not None and (prod := self.models.get(topics[1].lower(), None)) is not None:
                self.devices[deviceId] = device = prod[1](self.hass, deviceId, sn, prod[0], topics[1])
                if isinstance(device, ZendureDevice):
                    self.distribution.devices.append(device)
                _LOGGER.info("New device found: %s => %s", deviceId, msg.topic)
            else:
                _LOGGER.debug("Unknown device: %s => %s", deviceId, msg.topic)

        except Exception as err:
            _LOGGER.error(f"Error mqtt_message_received {err}!")
            _LOGGER.error(traceback.format_exc())
