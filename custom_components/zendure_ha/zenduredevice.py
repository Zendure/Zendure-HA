"""Zendure Integration device."""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.components.number import NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.template import Template
from paho.mqtt import client as mqtt_client

from custom_components.zendure_ha.binary_sensor import ZendureBinarySensor
from custom_components.zendure_ha.const import DOMAIN, BatteryState, SmartMode
from custom_components.zendure_ha.number import ZendureNumber
from custom_components.zendure_ha.select import ZendureRestoreSelect, ZendureSelect
from custom_components.zendure_ha.sensor import ZendureSensor
from custom_components.zendure_ha.switch import ZendureSwitch

_LOGGER = logging.getLogger(__name__)


class ZendureDevice:
    """A Zendure Device."""

    devicedict: dict[str, ZendureDevice] = {}
    devices: list[ZendureDevice] = []
    clusters: list[ZendureDevice] = []
    _messageid = 0

    def __init__(self, hass: HomeAssistant, h_id: str, h_prod: str, name: str, model: str) -> None:
        """Initialize ZendureDevice."""
        self._hass = hass
        self.hid = h_id
        self.prodkey = h_prod
        self.name = name
        self.unique = "".join(name.split())
        self.attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.name)},
            name=self.name,
            manufacturer="Zendure",
            model=model,
        )
        self._topic_read = f"iot/{self.prodkey}/{self.hid}/properties/read"
        self._topic_write = f"iot/{self.prodkey}/{self.hid}/properties/write"
        self.topic_function = f"iot/{self.prodkey}/{self.hid}/function/invoke"
        self.mqtt: mqtt_client.Client
        self.entities: dict[str, Entity | None] = {}
        self.batteries: list[str] = []
        self.devices.append(self)

        self.lastUpdate = datetime.now()
        self.waitTime = datetime.min
        self.powerMax = 0
        self.powerMin = 0
        self.powerAct = 0
        self.powerSp = 0
        self.capacity = 0
        self.clusterType: Any = 0
        self.clusterdevices: list[ZendureDevice] = []

    def updateProperty(self, key: Any, value: Any) -> bool:
        if (entity := self.entities.get(key, None)) is None:
            if key.endswith("Switch"):
                entity = self.binary(key, None, "switch")
            elif key.endswith(("Temperature", "Temp")):
                entity = self.sensor(key, "{{ (value | float/10 - 273.15) | round(2) }}", "°C", "temperature")
            elif key.endswith(( "totalVol", "minVol", "maxVol" )):
                entity = self.sensor(key, "{{ (value / 100) }}", "V", "voltage")
            elif key == "batCur":
                entity = self.sensor(key, "{{ (value / 10) }}")
            elif key.endswith("PowerCycle"):
                entity = None
            else:
                entity = ZendureSensor(self.attr_device_info, key)
            self.entities[key] = entity
            if entity is not None:
                self._hass.loop.call_soon_threadsafe(self.sensorAdd, entity, value)
            return False

        if entity is not None and entity.platform and entity.state != value:
            _LOGGER.info(f"Update {self.name} {key} => {value}")
            entity.update_value(value)
            return True
        return False

    def sensorAdd(self, entity: Entity, value: Any) -> None:
        try:
            _LOGGER.info(f"Add sensor: {entity.unique_id}")
            ZendureSensor.addSensors([entity])

            if entity.state != value:
                entity.update_value(value)

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())

    def updateBattery(self, data: list[int]) -> None:
        batPct = data[0]

        # _LOGGER.info(f"update_battery: {self.name} => {data}")
        # for i in range(data[1]):

        #     def value(idx: int) -> int:
        #         return data[idx * 4 + 2 + i]

        #     soc = value(0)
        #     vollt = value(1) * 10
        #     curr = value(2) / 10
        #     temp = value(8)
        #     _LOGGER.info(f"update_battery cell: {i} => {soc} {vollt} {curr} {temp}")

        # _LOGGER.info(f"update_battery: {self.hid} => {batPct}")

    def sensorsCreate(self) -> None:
        selects = [
            self.select(
                "acMode",
                {1: "input", 2: "output"},
                self.update_ac_mode,
            )
        ]

        if len(self.devices) > 1:
            clusters: dict[Any, str] = {0: "clusterunknown", 1: "clusterowncircuit", 2: "cluster800", 3: "cluster1200", 4: "cluster2400"}
            for d in self.devices:
                if d != self:
                    clusters[d.hid] = f"Part of {d.name} cluster"
            selects.append(
                self.select(
                    "cluster",
                    clusters,
                    self.update_cluster,
                    True,
                )
            )
        ZendureSelect.addSelects(selects)

    def update_ac_mode(self, mode: int) -> None:
        if mode == AcMode.INPUT:
            self.writeProperties({"acMode": mode, "inputLimit": self.entities["inputLimit"].state})
        elif mode == AcMode.OUTPUT:
            self.writeProperties({"acMode": mode, "outputLimit": self.entities["outputLimit"].state})

    def update_cluster(self, cluster: Any) -> None:
        try:
            _LOGGER.info(f"Update cluster: {self.name} => {cluster}")
            self.clusterType = cluster

            for d in self.devices:
                if self in d.clusterdevices:
                    if d.hid != cluster:
                        _LOGGER.info(f"Remove {self.name} from cluster {d.name}")
                        if self in d.clusterdevices:
                            d.clusterdevices.remove(self)
                elif d.hid == cluster:
                    _LOGGER.info(f"Add {self.name} to cluster {d.name}")
                    if self not in d.clusterdevices:
                        d.clusterdevices.append(self)

            if cluster in [1, 2, 3, 4] and self not in self.clusters:
                self.clusters.append(self)
                if self not in self.clusterdevices:
                    self.clusterdevices.append(self)

        except Exception as err:
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())

    def sendRefresh(self) -> None:
        self.mqtt.publish(self._topic_read, '{"properties": ["getAll"]}')

    def writeProperty(self, entity: Entity, value: Any) -> None:
        _LOGGER.info(f"Writing property {self.name} {entity.name} => {value}")
        ZendureDevice._messageid += 1
        if entity.unique_id is None:
            _LOGGER.error(f"Entity {entity.name} has no unique_id.")
            return

        property_name = entity.unique_id[(len(self.name) + 1) :]
        if property_name in {"minSoc", "socSet"}:
            value = int(value * 10)

        self.writeProperties({property_name: value})

    def writeProperties(self, props: dict[str, Any]) -> None:
        ZendureDevice._messageid += 1
        payload = json.dumps(
            {
                "deviceId": self.hid,
                "messageId": ZendureDevice._messageid,
                "timestamp": int(datetime.now().timestamp()),
                "properties": props,
            },
            default=lambda o: o.__dict__,
        )
        self.mqtt.publish(self._topic_write, payload)

    def function_invoke(self, command: Any) -> None:
        ZendureDevice._messageid += 1
        payload = json.dumps(
            command,
            default=lambda o: o.__dict__,
        )
        self.mqtt.publish(self.topic_function, payload)

    def binary(
        self,
        uniqueid: str,
        template: str | None = None,
        deviceclass: Any | None = None,
    ) -> ZendureBinarySensor:
        tmpl = Template(template, self._hass) if template else None
        s = ZendureBinarySensor(self.attr_device_info, uniqueid, tmpl, deviceclass)
        self.entities[uniqueid] = s
        return s

    def number(
        self,
        uniqueid: str,
        template: str | None = None,
        uom: str | None = None,
        deviceclass: Any | None = None,
        minimum: int = 0,
        maximum: int = 2000,
        mode: NumberMode = NumberMode.AUTO,
    ) -> ZendureNumber:
        def _write_property(entity: Entity, value: Any) -> None:
            self.writeProperty(entity, value)

        tmpl = Template(template, self._hass) if template else None
        s = ZendureNumber(
            self.attr_device_info,
            uniqueid,
            _write_property,
            tmpl,
            uom,
            deviceclass,
            maximum,
            minimum,
            mode,
        )
        self.entities[uniqueid] = s
        return s

    def select(self, uniqueid: str, options: dict[int, str], onwrite: Callable, persistent: bool = False) -> ZendureSelect:
        if persistent:
            s = ZendureRestoreSelect(self.attr_device_info, uniqueid, options, onwrite)
        else:
            s = ZendureSelect(self.attr_device_info, uniqueid, options, onwrite)
        self.entities[uniqueid] = s
        return s

    def sensor(
        self,
        uniqueid: str,
        template: str | None = None,
        uom: str | None = None,
        deviceclass: Any | None = None,
    ) -> ZendureSensor:
        tmpl = Template(template, self._hass) if template else None
        s = ZendureSensor(self.attr_device_info, uniqueid, tmpl, uom, deviceclass)
        self.entities[uniqueid] = s
        return s

    def switch(
        self,
        uniqueid: str,
        template: str | None = None,
        deviceclass: Any | None = None,
    ) -> ZendureSwitch:
        def _write_property(entity: Entity, value: Any) -> None:
            self.writeProperty(entity, value)

        tmpl = Template(template, self._hass) if template else None
        s = ZendureSwitch(self.attr_device_info, uniqueid, _write_property, tmpl, deviceclass)
        self.entities[uniqueid] = s
        return s

    def asInt(self, name: str) -> int:
        if (sensor := self.entities.get(name, None)) and sensor.state:
            return int(sensor.state)
        return 0

    def isInt(self, name: str) -> int | None:
        if (sensor := self.entities.get(name, None)) and sensor.state:
            return int(sensor.state)
        return None

    def asFloat(self, name: str) -> float:
        if (sensor := self.entities.get(name, None)) and sensor.state:
            return float(sensor.state)
        return 0

    def isEqual(self, name: str, value: Any) -> bool:
        if (sensor := self.entities.get(name, None)) and sensor.state:
            return sensor.state == value
        return False

    def powerState(self, _state: BatteryState) -> None:
        """Update the state of the manager."""
        return

    def powerSet(self, power: int) -> None:
        _LOGGER.info(f"Update power {self.name} => {power}")

    def powerActual(self, power: int) -> None:
        """Update the actual power."""
        self.powerAct = power

        if self.waitTime != datetime.min and abs(self.powerAct - self.powerSp) < 5:
            self.waitTime = datetime.min
            _LOGGER.info(f"Setpoint reached {self.name} => {power}")

    def clusterSet(self, state: BatteryState, power: int) -> None:
        _LOGGER.info(f"Update cluster {self.clusterType} power {self.name} => {power}")

        active = sorted(self.clusterdevices, key=lambda d: d.capacity, reverse=power > self.clusterMax / 2)
        capacity = self.clustercapacity
        for d in active:
            pwr = int(power * d.capacity / capacity) if capacity > 0 else 0
            capacity -= d.capacity
            pwr = max(0, min(d.powerMax, pwr)) if state == BatteryState.DISCHARGING else min(0, max(d.powerMin, pwr))
            if abs(pwr) > 0:
                if capacity == 0:
                    pwr = max(0, min(d.powerMax, power)) if state == BatteryState.DISCHARGING else min(0, max(d.powerMin, power))
                elif abs(pwr) > SmartMode.START_POWER or (abs(pwr) > SmartMode.MIN_POWER and d.powerAct != 0):
                    power -= pwr
                else:
                    pwr = 0

            # update the device
            d.powerSet(pwr)

    @property
    def clustercapacity(self) -> int:
        """Get the capacity of the cluster."""
        if self.clusterType == 0:
            return 0
        return sum(d.capacity for d in self.clusterdevices)

    @property
    def clusterMax(self) -> int:
        """Get the maximum power of the cluster."""
        cmax = sum(d.powerMax for d in self.clusterdevices)
        match self.clusterType:
            case 1:
                cmax = min(cmax, 3600)
            case 2:
                cmax = min(cmax, 800)
            case 3:
                cmax = min(cmax, 1200)
            case 4:
                cmax = min(cmax, 2400)
            case _:
                return 0
        return cmax

    @property
    def clusterMin(self) -> int:
        """Get the maximum power of the cluster."""
        cmin = sum(d.powerMin for d in self.clusterdevices)
        match self.clusterType:
            case 1:
                cmin = min(cmin, -3600)
            case 2:
                cmin = min(cmin, -2400)
            case 3:
                cmin = min(cmin, -2400)
            case 4:
                cmin = min(cmin, -3600)
            case _:
                return 0
        return cmin


class AcMode:
    INPUT = 1
    OUTPUT = 2
