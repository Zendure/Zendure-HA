import pytest
from datetime import datetime
from unittest.mock import AsyncMock

pytest.importorskip("homeassistant")

from custom_components.zendure_ha.manager import ZendureManager
from custom_components.zendure_ha.const import SmartMode, DeviceState, ManagerState


class DummyVal:
    def __init__(self, val):
        self.val = val

    @property
    def asInt(self):
        return self.val

    @property
    def asNumber(self):
        return self.val


class DummyByPass:
    def __init__(self, is_on=False):
        self.is_on = is_on


class DummySensor:
    def __init__(self):
        self.value = None

    def update_value(self, val):
        self.value = val

    @property
    def asNumber(self):
        return self.value

    @property
    def asInt(self):
        return int(self.value)


class DummyDevice:
    def __init__(self, pack_in, output_pack, solar_in, avail_kwh=0, bypass=False):
        self.packInputPower = DummyVal(pack_in)
        self.outputPackPower = DummyVal(output_pack)
        self.solarInputPower = DummyVal(solar_in)
        self.availableKwh = DummyVal(avail_kwh)
        self.byPass = DummyByPass(bypass)
        self.state = DeviceState.OFFLINE

    async def power_get(self):
        return True


@pytest.mark.asyncio
async def test_powerChanged_net_charge():
    manager = ZendureManager.__new__(ZendureManager)
    manager.devices = [DummyDevice(500, 100, 0)]
    manager.power = DummySensor()
    manager.availableKwh = DummySensor()
    manager.operation = SmartMode.MATCHING_DISCHARGE
    manager.state = ManagerState.IDLE
    manager.powerUpdate = AsyncMock()

    await manager.powerChanged(100, datetime.now())

    assert manager.power.value == 400
    manager.powerUpdate.assert_awaited_once_with(500, 0)


@pytest.mark.asyncio
async def test_powerChanged_net_feed():
    manager = ZendureManager.__new__(ZendureManager)
    manager.devices = [DummyDevice(100, 400, 0)]
    manager.power = DummySensor()
    manager.availableKwh = DummySensor()
    manager.operation = SmartMode.MATCHING_CHARGE
    manager.state = ManagerState.IDLE
    manager.powerUpdate = AsyncMock()

    await manager.powerChanged(-50, datetime.now())

    assert manager.power.value == -300
    manager.powerUpdate.assert_awaited_once_with(-350, 0)


@pytest.mark.asyncio
async def test_powerChanged_solar_charge():
    manager = ZendureManager.__new__(ZendureManager)
    manager.devices = [DummyDevice(0, 0, 200)]
    manager.power = DummySensor()
    manager.availableKwh = DummySensor()
    manager.operation = SmartMode.MATCHING_DISCHARGE
    manager.state = ManagerState.IDLE
    manager.powerUpdate = AsyncMock()

    await manager.powerChanged(0, datetime.now())

    assert manager.power.value == 200
    manager.powerUpdate.assert_awaited_once_with(200, 200)
