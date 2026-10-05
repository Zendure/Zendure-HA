"""The app reaches a local device through the cloud: requests go in, reports go out only for a while after a request."""

import json
import time
from types import SimpleNamespace

import custom_components.zendure_ha.zendure.driver as driver_module
from custom_components.zendure_ha.distribution import Distribution
from custom_components.zendure_ha.zendure.device import ConnectionType
from custom_components.zendure_ha.zendure.driver import BRIDGE_S, ZendureDriver


class FakeClient:
    instances: list = []

    def __init__(self, *args, userdata=None, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.userdata = userdata
        self.published: list[tuple[str, dict]] = []
        self.subscribed: list[str] = []
        self.credentials = None
        self.host = None
        FakeClient.instances.append(self)

    def username_pw_set(self, user, psw) -> None:  # noqa: ANN001
        self.credentials = (user, psw)

    def connect_async(self, host, port) -> None:  # noqa: ANN001
        self.host = (host, port)

    def loop_start(self) -> None:
        self.on_connect(self, self.userdata, None, 0, None)

    def subscribe(self, topic) -> None:  # noqa: ANN001
        self.subscribed.append(topic)

    def publish(self, topic, payload) -> None:  # noqa: ANN001
        self.published.append((topic, json.loads(payload)))

    def disconnect(self) -> None:
        return

    def loop_stop(self) -> None:
        return


def message(topic: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(topic=topic, payload=json.dumps(payload).encode())


async def test_bridge_forwards_requests_in_and_reports_out_for_a_while(hass, entry, monkeypatch) -> None:  # noqa: ANN001
    FakeClient.instances.clear()
    monkeypatch.setattr(driver_module.mqtt, "Client", FakeClient)
    driver = ZendureDriver(hass, entry.entry_id, [], Distribution(hass, entry.entry_id))
    driver.cloud_url, driver._cloud_port = "mq.zen-iot.com", 1883
    local = FakeClient(userdata=ConnectionType.LOCAL)
    driver._mqtt_clients[ConnectionType.LOCAL] = local
    device = driver.init_device(None, "Hyper2000_22", {"product": "Hyper 2000", "sn": "EOK1GDA3K22", "device_id": "dev1", "prodkey": "gDa3tb"})
    report = message("/gDa3tb/dev1/properties/report", {"properties": {"electricLevel": 50}})

    # the first local report opens the bridge client, logged in as the device; nothing is forwarded yet
    driver._on_mqtt_message(local, ConnectionType.LOCAL, report)
    bridge = driver._bridges["dev1"]
    assert bridge.credentials == ("dev1", "F5AC3E289E317D46") and bridge.host == ("mq.zen-iot.com", 1883) and bridge.subscribed == ["iot/gDa3tb/dev1/#"]
    assert bridge.published == [] and device.soc == 50

    # the app asks something: the request lands on the local broker and the window opens
    driver._on_bridge_message(bridge, device, message("iot/gDa3tb/dev1/properties/read", {"properties": ["getAll"]}))
    assert local.published == [("iot/gDa3tb/dev1/properties/read", {"properties": ["getAll"]})]
    driver._on_mqtt_message(local, ConnectionType.LOCAL, report)
    assert bridge.published == [("/gDa3tb/dev1/properties/report", {"properties": {"electricLevel": 50}, "isHA": True})]

    # our own copy coming back, and the cloud's time-sync, do nothing
    driver._on_mqtt_message(bridge, ConnectionType.CLOUD, message("/gDa3tb/dev1/properties/report", {"properties": {"electricLevel": 1}, "isHA": True}))
    driver._on_bridge_message(bridge, device, message("iot/gDa3tb/dev1/time-sync", {"messageId": 1}))
    assert device.soc == 50 and device.connection == ConnectionType.LOCAL and len(local.published) == 1

    # the window closes BRIDGE_S after the last request
    driver._bridge_until["dev1"] = time.time() - 1
    driver._on_mqtt_message(local, ConnectionType.LOCAL, report)
    assert len(bridge.published) == 1
    assert BRIDGE_S == 300
    device.unload()
