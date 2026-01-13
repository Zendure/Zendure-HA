"""Base class for Zendure entities."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity, EntityPlatformState
from stringcase import snakecase

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class ZendureEntity(Entity):
    """Base entity for all Zendure entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        device: ZendureEntities,
        uniqueid: str,
        entitytype: str,
    ) -> None:
        """Initialize a Zendure entity."""
        self._attr_has_entity_name = True
        self._attr_should_poll = False
        self._attr_available = True
        self.device_info = device.attr_device_info
        self._attr_unique_id = f"{device.name}-{uniqueid}"
        self.entity_id = f"{entitytype}.{device.name}-{snakecase(uniqueid)}"
        self._attr_translation_key = snakecase(uniqueid)

    def update_value(self, _value: Any) -> bool:
        """Update the entity value."""
        return False

    @property
    def hasPlatform(self) -> bool:
        """Return whether the entity has a platform."""
        return self._platform_state != EntityPlatformState.NOT_ADDED


class ZendureEntities:
    def __init__(self, hass: HomeAssistant, model: str, device_id: str | None = None, device_sn: str | None = None, model_id: str | None = None, parent: str | None = None) -> None:
        """Initialize the Zendure device."""
        self.hass = hass
        self.name = f"{model.replace(' ', '').replace('SolarFlow', 'SF')} {device_sn[-2:] if device_sn is not None else ''}".strip()

        self.prodKey = model_id if model_id is not None else ""
        self.deviceId = device_id
        self.lastseen = datetime.min
        self.attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.name)},
            name=self.name,
            manufacturer="Zendure",
            model=model,
            model_id=model_id,
            hw_version=device_id,
            serial_number=device_sn,
        )

        if device_sn is not None:
            device_registry = dr.async_get(self.hass)
            device_entry = device_registry.async_get_device(identifiers={(DOMAIN, self.name)})
            if device_entry is not None:
                self.attr_device_info["connections"] = device_entry.connections

        if parent is not None:
            self.attr_device_info["via_device"] = (DOMAIN, parent)

        self._messageid = 0
        self.topic_function = f"iot/{model_id}/{self.deviceId}/function/invoke"
        self.topic_read = f"iot/{model_id}/{self.deviceId}/properties/read"
        self.topic_write = f"iot/{model_id}/{self.deviceId}/properties/write"
        self.ready = datetime.min

    def refresh(self) -> None:
        return

    def setStatus(self, _lastseen: datetime | None = None) -> None:
        """Set the device connection status."""

    def entityRead(self, payload: dict) -> None:
        """Handle incoming MQTT message for the device."""

    def mqttRegister(self, payload: dict) -> None:
        """Handle device registration."""
        if (params := payload.get("params")) is not None and (token := params.get("token")) is not None:
            self.mqttPublish(f"iot/{self.prodKey}/{self.deviceId}/register/replay", {"token": token, "result": 0})
        else:
            _LOGGER.warning(f"MQTT register failed for device {self.name}: no token in payload")

    def mqttPublish(self, topic: str, command: Any) -> None:
        self._messageid += 1
        command["messageId"] = self._messageid
        command["deviceId"] = self.deviceId
        command["timestamp"] = int(datetime.now().timestamp())
        payload = json.dumps(command, default=lambda o: o.__dict__)
        mqtt.publish(self.hass, topic, payload=payload)

    def mqttInvoke(self, command: Any) -> None:
        self.mqttPublish(self.topic_function, command)

    @property
    def bleMac(self) -> str:
        for conn in self.attr_device_info.get("connections", []):
            if conn[0] == dr.CONNECTION_BLUETOOTH:
                return conn[1]
        return ""

    @bleMac.setter
    def bleMac(self, value: str) -> None:
        self.attr_device_info["connections"] = {(CONNECTION_BLUETOOTH, value)}
        device_registry = dr.async_get(self.hass)
        device_entry = device_registry.async_get_device(identifiers={(DOMAIN, self.name)})
        if device_entry is not None:
            device_registry.async_update_device(device_entry.id, merge_connections={(CONNECTION_BLUETOOTH, value)})

    @property
    def snNumber(self) -> str:
        return self.attr_device_info.get("serial_number") or ""

    @property
    def sw_version(self) -> str:
        return self.attr_device_info.get("sw_version") or ""

    @sw_version.setter
    def sw_version(self, value: str) -> None:
        self.attr_device_info["sw_version"] = value
        device_registry = dr.async_get(self.hass)
        device_entry = device_registry.async_get_device(identifiers={(DOMAIN, self.name)})
        if device_entry is not None:
            device_registry.async_update_device(device_entry.id, sw_version=value)
