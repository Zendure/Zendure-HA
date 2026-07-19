# ruff: noqa: PT009, PT018, PT027, S101, SLF001
"""Concurrency tests for the Zendure manager control lock."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path

ROOT = Path(os.environ.get("ZENDURE_TEST_ROOT", Path(__file__).parents[1]))


def _stub_module(name: str, **attributes: object) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules[name] = module
    return module


class _Generic:
    @classmethod
    def __class_getitem__(cls, _item: object) -> type[_Generic]:
        return cls


def _load_manager_module() -> types.ModuleType:
    """Load the real manager module with lightweight HA dependency stubs."""
    for package in (
        "homeassistant",
        "homeassistant.auth",
        "homeassistant.auth.providers",
        "homeassistant.components",
        "homeassistant.helpers",
        "custom_components",
        "custom_components.zendure_ha",
    ):
        module = _stub_module(package)
        module.__path__ = []

    _stub_module("homeassistant.auth.const", GROUP_ID_USER="user")
    _stub_module("homeassistant.auth.providers.homeassistant", HassAuthProvider=object)
    _stub_module("homeassistant.components.bluetooth", BluetoothServiceInfoBleak=object)
    _stub_module(
        "homeassistant.components.persistent_notification",
        async_create=lambda *_args, **_kwargs: None,
    )
    _stub_module(
        "homeassistant.components.number", NumberMode=types.SimpleNamespace(BOX="box")
    )
    _stub_module("homeassistant.config_entries", ConfigEntry=_Generic)
    _stub_module(
        "homeassistant.core",
        Event=_Generic,
        EventStateChangedData=object,
        HomeAssistant=object,
    )
    _stub_module(
        "homeassistant.helpers.event",
        async_track_state_change_event=lambda *_args, **_kwargs: None,
    )

    class _Coordinator(_Generic):
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    _stub_module(
        "homeassistant.helpers.update_coordinator", DataUpdateCoordinator=_Coordinator
    )

    async def _integration(*_args: object, **_kwargs: object) -> None:
        return None

    _stub_module("homeassistant.loader", async_get_integration=_integration)

    package = sys.modules["custom_components.zendure_ha"]
    package.__path__ = [str(ROOT / "custom_components" / "zendure_ha")]

    const_spec = importlib.util.spec_from_file_location(
        "custom_components.zendure_ha.const",
        ROOT / "custom_components" / "zendure_ha" / "const.py",
    )
    assert const_spec is not None and const_spec.loader is not None
    const_module = importlib.util.module_from_spec(const_spec)
    sys.modules[const_spec.name] = const_module
    const_spec.loader.exec_module(const_module)

    class _Api:
        pass

    class _Device:
        pass

    class _DeviceSettings:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    class _EntityDevice:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    class _FuseGroup:
        pass

    class _Entity:
        pass

    _stub_module("custom_components.zendure_ha.api", Api=_Api)
    _stub_module(
        "custom_components.zendure_ha.device",
        DeviceSettings=_DeviceSettings,
        ZendureDevice=_Device,
        ZendureLegacy=_Device,
    )
    _stub_module("custom_components.zendure_ha.entity", EntityDevice=_EntityDevice)
    _stub_module("custom_components.zendure_ha.fusegroup", FuseGroup=_FuseGroup)
    _stub_module("custom_components.zendure_ha.number", ZendureRestoreNumber=_Entity)
    _stub_module(
        "custom_components.zendure_ha.select",
        ZendureRestoreSelect=_Entity,
        ZendureSelect=_Entity,
    )
    _stub_module("custom_components.zendure_ha.sensor", ZendureSensor=_Entity)

    manager_spec = importlib.util.spec_from_file_location(
        "custom_components.zendure_ha.manager",
        ROOT / "custom_components" / "zendure_ha" / "manager.py",
    )
    assert manager_spec is not None and manager_spec.loader is not None
    manager_module = importlib.util.module_from_spec(manager_spec)
    sys.modules[manager_spec.name] = manager_module
    manager_spec.loader.exec_module(manager_module)
    return manager_module


MANAGER_MODULE = _load_manager_module()
ManagerMode = MANAGER_MODULE.ManagerMode
ZendureManager = MANAGER_MODULE.ZendureManager


class _OperationEntity:
    def __init__(self, value: int) -> None:
        self.value = value


class _Device:
    def __init__(self, power_off: Callable[[], Awaitable[None]]) -> None:
        self.online = True
        self.power_off = power_off


class _FuseGroup:
    def __init__(self) -> None:
        self.initPower = False


class _SimulatedControlError(RuntimeError):
    """Control-loop failure used to verify lock release."""


class _Hass:
    is_running = True


def _manager() -> ZendureManager:
    manager = ZendureManager(object(), object())
    manager.operation = ManagerMode.MATCHING
    manager.p1meterEvent = object()
    manager.devices = []
    manager.hass = object()
    manager.charge = [object()]
    manager.charge_limit = 1
    manager.charge_optimal = 1
    manager.charge_weight = 1
    manager.discharge = [object()]
    manager.discharge_bypass = 1
    manager.discharge_limit = 1
    manager.discharge_optimal = 1
    manager.discharge_produced = 1
    manager.discharge_weight = 1
    manager.idle = [object()]
    manager.idle_lvlmax = 1
    manager.idle_lvlmin = 1
    manager.produced = 1
    manager.fuseGroups = [_FuseGroup()]
    return manager


class ManagerControlLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_p1_callback_cannot_finish_after_off(self) -> None:
        manager = _manager()
        manager.hass = _Hass()
        order: list[str] = []
        control_started = asyncio.Event()
        release_control = asyncio.Event()

        async def power_changed(*_args: object) -> None:
            order.append("control-start")
            control_started.set()
            await release_control.wait()
            order.append("power-command")

        async def power_off() -> None:
            order.append("off-command")

        manager.powerChanged = power_changed
        manager.devices = [_Device(power_off)]
        event = types.SimpleNamespace(
            data={"new_state": types.SimpleNamespace(state="500")}
        )

        p1_task = asyncio.create_task(manager._p1_changed(event))
        await control_started.wait()
        off_task = asyncio.create_task(
            manager.update_operation(_OperationEntity(ManagerMode.OFF.value), None)
        )
        await asyncio.sleep(0)

        release_control.set()
        await asyncio.gather(p1_task, off_task)

        self.assertEqual(
            ["control-start", "power-command", "off-command"],
            order,
        )

    async def test_off_command_is_last_when_control_cycle_is_in_flight(self) -> None:
        manager = _manager()
        order: list[str] = []
        control_started = asyncio.Event()
        release_control = asyncio.Event()

        async def power_changed(*_args: object) -> None:
            order.append("control-start")
            control_started.set()
            await release_control.wait()
            order.append("power-command")

        async def power_off() -> None:
            order.append("off-command")

        manager.powerChanged = power_changed
        manager.devices = [_Device(power_off)]

        control_task = asyncio.create_task(
            manager._execute_control_update(500, False, MANAGER_MODULE.datetime.now())
        )
        await control_started.wait()
        off_task = asyncio.create_task(
            manager.update_operation(_OperationEntity(ManagerMode.OFF.value), None)
        )
        await asyncio.sleep(0)

        self.assertFalse(off_task.done())
        release_control.set()
        await asyncio.gather(control_task, off_task)

        self.assertEqual(["control-start", "power-command", "off-command"], order)
        self.assertEqual(ManagerMode.OFF, manager.operation)

    async def test_control_cycle_waits_for_off_and_cannot_reactivate_device(
        self,
    ) -> None:
        manager = _manager()
        order: list[str] = []
        off_started = asyncio.Event()
        release_off = asyncio.Event()
        control_started = asyncio.Event()

        async def power_off() -> None:
            order.append("off-start")
            off_started.set()
            await release_off.wait()
            order.append("off-command")

        async def power_changed(*_args: object) -> None:
            control_started.set()
            order.append(
                "off-cycle" if manager.operation == ManagerMode.OFF else "power-command"
            )

        manager.devices = [_Device(power_off)]
        manager.powerChanged = power_changed

        off_task = asyncio.create_task(
            manager.update_operation(_OperationEntity(ManagerMode.OFF.value), None)
        )
        await off_started.wait()
        control_task = asyncio.create_task(
            manager._execute_control_update(500, False, MANAGER_MODULE.datetime.now())
        )
        await asyncio.sleep(0)

        self.assertFalse(control_started.is_set())
        release_off.set()
        await asyncio.gather(off_task, control_task)

        self.assertEqual(["off-start", "off-command", "off-cycle"], order)
        self.assertNotIn("power-command", order)

    async def test_off_runs_after_all_control_cycles_already_queued(self) -> None:
        manager = _manager()
        order: list[str] = []
        first_control_started = asyncio.Event()
        release_first_control = asyncio.Event()

        async def power_changed(p1: int, *_args: object) -> None:
            order.append(f"control-{p1}")
            if p1 == 0:
                first_control_started.set()
                await release_first_control.wait()

        async def power_off() -> None:
            order.append("off")

        manager.powerChanged = power_changed
        manager.devices = [_Device(power_off)]
        control_tasks = [
            asyncio.create_task(
                manager._execute_control_update(
                    p1,
                    False,
                    MANAGER_MODULE.datetime.now(),
                )
            )
            for p1 in range(5)
        ]
        await first_control_started.wait()
        off_task = asyncio.create_task(
            manager.update_operation(_OperationEntity(ManagerMode.OFF.value), None)
        )
        await asyncio.sleep(0)

        release_first_control.set()
        await asyncio.gather(*control_tasks, off_task)

        self.assertEqual(
            ["control-0", "control-1", "control-2", "control-3", "control-4", "off"],
            order,
        )

    async def test_control_state_reset_is_unchanged(self) -> None:
        manager = _manager()
        snapshot: dict[str, object] = {}

        async def power_changed(*_args: object) -> None:
            snapshot.update(
                charge=manager.charge.copy(),
                charge_limit=manager.charge_limit,
                charge_optimal=manager.charge_optimal,
                charge_weight=manager.charge_weight,
                discharge=manager.discharge.copy(),
                discharge_bypass=manager.discharge_bypass,
                discharge_limit=manager.discharge_limit,
                discharge_optimal=manager.discharge_optimal,
                discharge_produced=manager.discharge_produced,
                discharge_weight=manager.discharge_weight,
                idle=manager.idle.copy(),
                idle_lvlmax=manager.idle_lvlmax,
                idle_lvlmin=manager.idle_lvlmin,
                produced=manager.produced,
                fuse_init=manager.fuseGroups[0].initPower,
            )

        manager.powerChanged = power_changed
        await manager._execute_control_update(100, False, MANAGER_MODULE.datetime.now())

        self.assertEqual(
            {
                "charge": [],
                "charge_limit": 0,
                "charge_optimal": 0,
                "charge_weight": 0,
                "discharge": [],
                "discharge_bypass": 0,
                "discharge_limit": 0,
                "discharge_optimal": 0,
                "discharge_produced": 0,
                "discharge_weight": 0,
                "idle": [],
                "idle_lvlmax": 0,
                "idle_lvlmin": 100,
                "produced": 0,
                "fuse_init": True,
            },
            snapshot,
        )

    async def test_non_off_mode_change_does_not_power_off_devices(self) -> None:
        manager = _manager()
        power_off_calls = 0

        async def power_off() -> None:
            nonlocal power_off_calls
            power_off_calls += 1

        manager.devices = [_Device(power_off)]
        await manager.update_operation(_OperationEntity(ManagerMode.MANUAL.value), None)

        self.assertEqual(ManagerMode.MANUAL, manager.operation)
        self.assertEqual(0, power_off_calls)

    async def test_off_still_powers_devices_off_without_p1_listener(self) -> None:
        manager = _manager()
        manager.p1meterEvent = None
        power_off_calls = 0

        async def power_off() -> None:
            nonlocal power_off_calls
            power_off_calls += 1

        manager.devices = [_Device(power_off)]
        await manager.update_operation(_OperationEntity(ManagerMode.OFF.value), None)

        self.assertEqual(ManagerMode.OFF, manager.operation)
        self.assertEqual(1, power_off_calls)

    async def test_exception_in_control_cycle_releases_lock(self) -> None:
        manager = _manager()
        power_off_called = asyncio.Event()

        async def power_changed(*_args: object) -> None:
            raise _SimulatedControlError

        async def power_off() -> None:
            power_off_called.set()

        manager.powerChanged = power_changed
        manager.devices = [_Device(power_off)]

        with self.assertRaises(_SimulatedControlError):
            await manager._execute_control_update(
                100, False, MANAGER_MODULE.datetime.now()
            )

        await asyncio.wait_for(
            manager.update_operation(_OperationEntity(ManagerMode.OFF.value), None),
            timeout=0.2,
        )
        self.assertTrue(power_off_called.is_set())


if __name__ == "__main__":
    unittest.main()
