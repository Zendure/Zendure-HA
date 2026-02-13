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
from .device import DeviceState, ZendureDevice
from .devices.ace1500 import ACE1500
from .devices.aio2400 import AIO2400
from .devices.hub1200 import Hub1200
from .devices.hub2000 import Hub2000
from .devices.hyper2000 import Hyper2000
from .devices.solarflow800 import SolarFlow800, SolarFlow800Plus, SolarFlow800Pro
from .devices.solarflow2400 import SolarFlow2400AC
from .devices.superbase import SuperBaseV4600, SuperBaseV6400
from .distribution import Distribution
from .entity import ZendureEntities, ZendureEntity
from .fusegroup import CONST_EMPTY_GROUP, FuseGroup
from .number import ZendureRestoreNumber
from .select import ZendureRestoreSelect
from .sensor import ZendureSensor
from .smartmeter import ZendureSmartMeter

_LOGGER = logging.getLogger(__name__)

type ZendureConfigEntry = ConfigEntry[ZendureCoordinator]

CONST_TOPIC_CNT = 4


class ZendureCoordinator(DataUpdateCoordinator[None], ZendureEntities):
    """Zendure coordinator."""

    def __init__(self, hass: HomeAssistant, entry: ZendureConfigEntry) -> None:
        """Initialize Zendure Coordinator."""
        from .fusegroup import FuseGroup

        super().__init__(hass, _LOGGER, name="Zendure Coordinator", update_interval=timedelta(seconds=2), config_entry=entry)
        ZendureEntities.__init__(self, self.hass, "Zendure Coordinator", "Zendure Coordinator")

        self.power = ZendureSensor(self, "power", None, "W", "power", "measurement", 0)
        self.distribution = Distribution(self.hass, entry.data.get("p1meter", ""), self.power)
        self.operationmode = ZendureRestoreSelect(self, "Operation", {0: "off", 1: "manual", 2: "smart", 3: "smart_discharging", 4: "smart_charging"}, self.update_operation)
        self.operationstate = ZendureSensor(self, "operation_state")
        self.bypassmode = ZendureRestoreSelect(self, "Bypass", {0: "never", 1: "house", 2: "grid"}, self.update_bypass)
        self.manualpower = ZendureRestoreNumber(self, "manual_power", self.update_manualpower, None, "W", "power", 12000, -12000, NumberMode.BOX, True)
        self.availableKwh = ZendureSensor(self, "available_kwh", None, "kWh", "energy", None, 1)
        self.fuseGroups: list[FuseGroup] = []
        self.next_update = datetime.min

    async def _async_setup(self) -> None:
        """Set up the coordinator."""
        _LOGGER.debug("Setting up Zendure coordinator for entry: %s", self.config_entry.entry_id)
        self.api = Api()
        try:
            device_registry = dr.async_get(self.hass)
            for d in dr.async_entries_for_config_entry(device_registry, self.config_entry.entry_id):
                if d.serial_number and d.model_id and d.hw_version:
                    model_key = d.model_id.lower()
                    if prod := Api.models.get(model_key):
                        sn = d.serial_number
                        deviceId = d.hw_version
                        device = prod[1](self.hass, deviceId, sn, prod[0], d.model_id)
                        Api.devices[deviceId] = device
                        if isinstance(device, ZendureDevice):
                            self.distribution.devices.append(device)
                    else:
                        _LOGGER.warning("Unknown model_id %s for device %s, skipping device setup", d.model_id, d.name)
        except Exception:
            _LOGGER.exception("Unexpected error in MQTT message handler")
        await self.update_fusegroups()
        await self.async_update(self.hass, self.config_entry)
        await self.api.async_init(self.hass, self.config_entry)

    async def async_update(self, _hass: HomeAssistant, entry: ZendureConfigEntry) -> None:
        """Handle options update."""
        _LOGGER.debug("async_update: %s", entry.entry_id)
        # Api.wifi_ssid = entry.data.get("wifissid", "")
        # Api.wifi_psw = entry.data.get("wifipsw", "")
        # Api.iotUrl = mqtt.
        #     _LOGGER.debug("Updating Zendure config entry: %s", entry.entry_id)
        #     Api.mqttLogging = entry.data.get(CONF_MQTTLOG, False)
        #     ZendureManager.simulation = entry.data.get(CONF_SIM, False)
        #     entry.runtime_data.update_p1meter(entry.data.get(CONF_P1METER, "sensor.power_actual"))

    async def async_unload(self) -> None:
        """Unload the coordinator."""
        _LOGGER.debug("async_unload: %s", self.config_entry.entry_id)

    async def _async_update_data(self) -> None:
        time = datetime.now() - timedelta(minutes=2)
        if doUpdate := datetime.now() > self.next_update:
            self.next_update = datetime.now() + timedelta(seconds=60)

        # for d in self.devices.values():
        #     if isinstance(d, ZendureDevice):
        #         if d.lastseen < time:
        #             if not d.usefallback or d.status != DeviceState.OFFLINE:
        #                 Api.fallback_start(d)
        #                 d.connectionStatus.update_value(DeviceState.OFFLINE.value)
        #         elif d.usefallback:
        #             await Api.fallback_stop(d)
        #         elif doUpdate:
        #             d.mqttPublish(d.topic_read, {"properties": ["getAll"]})

        # check for fallback devices
        # if len(Api.recover) > 0:
        #     await Api.fallback_check(self.hass)

        # Manually update the timer
        if self.hass and self.hass.loop.is_running():
            self._schedule_refresh()

    async def update_operation(self, entity: ZendureRestoreSelect, _operation: Any) -> None:
        self.distribution.set_operation(ManagerMode(entity.value))

    async def update_bypass(self, _entity: ZendureRestoreSelect, _operation: Any) -> None:
        _LOGGER.info("Update bypass")

    async def update_manualpower(self, _entity: Any, power: Any) -> None:
        self.distribution.manualpower = power

    async def update_fusegroups(self) -> None:
        # updateFuseGroup callback
        async def updateFuseGroup(_entity: ZendureEntity, _value: Any) -> None:
            await self.update_fusegroups()

        fuseGroups: dict[str, FuseGroup] = {}
        for device in Api.devices.values():
            if not isinstance(device, ZendureDevice):
                continue

            if device.fuseGroup.onchanged is None:
                device.fuseGroup.onchanged = updateFuseGroup

            def updateDevice(device: ZendureDevice, disable: bool) -> None:
                device.fuseGroupMax.update_disabled(self.hass, disable)
                device.fuseGroupMin.update_disabled(self.hass, disable)

            match device.fuseGroup.state:
                case "unused":
                    device.fuseGrp = CONST_EMPTY_GROUP
                    device.power_off()
                    updateDevice(device, True)
                    continue
                case "owncircuit":
                    device.fuseGrp = FuseGroup(device.name, 3600, -3600, [device])
                    updateDevice(device, True)
                    continue
                case "fusegroup":
                    device.fuseGrp = FuseGroup(device.name, device.fuseGroupMax.asInt, device.fuseGroupMin.asInt, [device])
                    fuseGroups[device.deviceId] = device.fuseGrp
                    updateDevice(device, False)
                case _:
                    updateDevice(device, True)

        # Update the fusegroups and select optins for each device
        for device in Api.devices.values():
            if not isinstance(device, ZendureDevice):
                continue
            try:
                fusegroups = ZendureDevice.fuseGroups.copy()
                for deviceId, fg in fuseGroups.items():
                    if deviceId != device.deviceId:
                        fusegroups[deviceId] = f"fusegroup: {fg.name}"
                device.fuseGroup.setDict(fusegroups)
            except AttributeError as err:
                _LOGGER.error("Device %s missing fuseGroup attribute: %s", device.name, err)
            except Exception as err:
                _LOGGER.error("Unable to update fusegroup options for device %s (%s): %s", device.name, device.deviceId, err, exc_info=True)

        # Add devices to fusegroups
        for device in Api.devices.values():
            if not isinstance(device, ZendureDevice):
                continue

            if fg := fuseGroups.get(device.fuseGroup.value):
                device.fuseGrp = fg
                fg.devices.append(device)

    # @callback
    # def mqtt_message_received(self, msg: mqtt.ReceiveMessage) -> None:
    #     """Handle Zendure mqtt messages."""
    #     if msg.payload is None or not msg.payload:
    #         return
    #     try:
    #         # Validate topic format before accessing indices
    #         if len(topics := msg.topic.split("/", 3)) < CONST_TOPIC_CNT:
    #             _LOGGER.warning("Invalid MQTT topic format: %s (expected 4 segments)", msg.topic)
    #             return

    #         # deserialize payload
    #         deviceId = topics[2]
    #         try:
    #             payload = json.loads(msg.payload)
    #         except json.JSONDecodeError as err:
    #             _LOGGER.error("Failed to decode JSON from device %s: %s", deviceId, err)
    #             return
    #         except UnicodeDecodeError as err:
    #             _LOGGER.error("Failed to decode payload encoding from device %s: %s", deviceId, err)
    #             return

    #         if (device := self.devices.get(deviceId, None)) is not None:
    #             match topics[3]:
    #                 case "properties/report":
    #                     device.entityRead(payload)
    #                 case "register":
    #                     device.mqttRegister(payload)
    #                 case "function/invoke/reply" | "properties/write/reply":
    #                     device.ready = datetime.min
    #                 case _:
    #                     pass

    #             # if self.mqttLogging:
    #             # _LOGGER.info("Topic: %s => %s", msg.topic.replace(device.deviceId, device.name).replace(device.snNumber, "snxxx"), payload)
    #         elif (lg := payload.get("log", None)) is not None and (sn := lg.get("sn", None)) is not None and (prod := self.models.get(topics[1].lower(), None)) is not None:
    #             self.devices[deviceId] = device = prod[1](self.hass, deviceId, sn, prod[0], topics[1])
    #             if isinstance(device, ZendureDevice):
    #                 self.distribution.devices.append(device)
    #             _LOGGER.info("New device found: %s => %s", deviceId, msg.topic)
    #         else:
    #             _LOGGER.debug("Unknown device: %s => %s", deviceId, msg.topic)

    #     except Exception as err:
    #         _LOGGER.error(f"Error mqtt_message_received {err}!")
    #         _LOGGER.error(traceback.format_exc())
