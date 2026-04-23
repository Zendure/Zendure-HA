"""MQTT protocol layer for Zendure."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .api import Api

_LOGGER = logging.getLogger(__name__)


def on_connect(api: Api, client: Any, userdata: Any, _flags: Any, rc: Any, _props: Any) -> None:
    """Handle MQTT client connection."""
    _LOGGER.info("Client %s connected to MQTT broker, return code: %s", userdata, rc)
    if userdata == "zendure":
        for device in api.devices.values():
            if client == device.zendure:
                client.subscribe(f"iot/{device.prodkey}/{device.deviceId}/#")
                api.mqttCloud.unsubscribe(f"/{device.prodkey}/{device.deviceId}/#")
                api.mqttCloud.unsubscribe(f"iot/{device.prodkey}/{device.deviceId}/#")
    else:
        for device in api.devices.values():
            client.subscribe(f"/{device.prodkey}/{device.deviceId}/#")
            client.subscribe(f"iot/{device.prodkey}/{device.deviceId}/#")


def on_disconnect(api: Api, _client: Any, userdata: Any, _flags: Any, rc: Any, _props: Any) -> None:
    """Handle MQTT client disconnection."""
    _LOGGER.info("Client %s disconnected to MQTT broker, return code: %s", userdata, rc)


def on_msg_cloud(api: Api, client: Any, _userdata: Any, msg: Any) -> None:
    """Handle MQTT message from cloud."""
    if msg.payload is None or not msg.payload:
        return
    try:
        topics = msg.topic.split("/", 3)

        # Validate topic format before accessing indices
        if len(topics) < 4:
            _LOGGER.warning("Invalid MQTT topic format: %s (expected 4 segments)", msg.topic)
            return

        deviceId = topics[2]

        if (device := api.devices.get(deviceId, None)) is not None:
            try:
                payload = json.loads(msg.payload.decode())
            except json.JSONDecodeError as err:
                _LOGGER.error("Failed to decode JSON from device %s: %s", deviceId, err)
                return
            except UnicodeDecodeError as err:
                _LOGGER.error("Failed to decode payload encoding from device %s: %s", deviceId, err)
                return

            if "isHA" in payload:
                return

            if api.mqttLogging:
                _LOGGER.info("Topic: %s => %s", msg.topic.replace(device.deviceId, device.name).replace(device.snNumber, "snxxx"), payload)

            if device.mqttMessage(topics[3], payload) and device.mqtt != client:
                device.mqtt = client
                device.setStatus()

        else:
            _LOGGER.debug("Unknown device: %s => %s", deviceId, msg.topic)

    except Exception:
        _LOGGER.exception("Unexpected error in MQTT cloud message handler")


def on_msg_local(api: Api, client: Any, _userdata: Any, msg: Any) -> None:
    """Handle MQTT message from local."""
    if msg.payload is None or not msg.payload or len(api.devices) == 0:
        return
    try:
        topics = msg.topic.split("/", 3)

        # Validate topic format before accessing indices
        if len(topics) < 4:
            _LOGGER.warning("Invalid local MQTT topic format: %s (expected 4 segments)", msg.topic)
            return

        deviceId = topics[2]

        if (device := api.devices.get(deviceId, None)) is not None:
            try:
                payload = json.loads(msg.payload.decode())
            except json.JSONDecodeError as err:
                _LOGGER.error("Failed to decode JSON from local device %s: %s", deviceId, err)
                return
            except UnicodeDecodeError as err:
                _LOGGER.error("Failed to decode local payload encoding from device %s: %s", deviceId, err)
                return

            if "isHA" in payload:
                return

            if api.mqttLogging:
                _LOGGER.info("Local topic: %s => %s", msg.topic.replace(device.deviceId, device.name).replace(device.snNumber, "snxxx"), payload)

            if device.mqttMessage(topics[3], payload):
                if device.mqtt != client:
                    device.mqtt = client
                    device.setStatus()

                if device.zendure is not None and device.zendure.is_connected():
                    payload["isHA"] = True
                    device.zendure.publish(msg.topic, json.dumps(payload, default=lambda o: o.__dict__))
        else:
            _LOGGER.debug("Local message from unknown device %s: %s", msg.topic, deviceId)

    except Exception:
        _LOGGER.exception("Unexpected error in MQTT local message handler")


def on_msg_device(api: Api, _client: Any, _userdata: Any, msg: Any) -> None:
    """Handle MQTT message from device."""
    if msg.payload is None or not msg.payload:
        return
    try:
        topics = msg.topic.split("/", 3)
        deviceId = topics[2]

        if api.devices.get(deviceId, None) is not None and topics[0] == "iot":
            api.mqttLocal.publish(msg.topic, msg.payload)

    except Exception as err:
        _LOGGER.error(err)
