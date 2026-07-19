# ruff: noqa: PLR2004, PT009, PT018, PT027, S101, SLF001
"""Power-command ordering and OFF reconciliation tests."""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import os
import sys
import types
import unittest
from collections.abc import Awaitable, Callable, Coroutine
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

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass


def _load_device_module() -> types.ModuleType:
    """Load the real device module with lightweight dependency stubs."""
    for package in (
        "homeassistant",
        "homeassistant.components",
        "homeassistant.helpers",
        "homeassistant.util",
        "paho",
        "paho.mqtt",
        "custom_components",
        "custom_components.zendure_ha",
    ):
        module = _stub_module(package)
        module.__path__ = []

    class _ClientTimeout:
        def __init__(self, **_kwargs: object) -> None:
            pass

    _stub_module("aiohttp", ClientTimeout=_ClientTimeout)
    _stub_module("bleak", BleakClient=_Generic)
    _stub_module("bleak.exc", BleakError=Exception)
    _stub_module("bleak_retry_connector", establish_connection=None)
    _stub_module("paho.mqtt.client", Client=_Generic)

    _stub_module("homeassistant.components.bluetooth")
    _stub_module(
        "homeassistant.components.persistent_notification",
        async_create=lambda *_args, **_kwargs: None,
    )
    _stub_module(
        "homeassistant.components.number",
        NumberMode=types.SimpleNamespace(SLIDER="slider"),
    )
    _stub_module("homeassistant.core", HomeAssistant=object)
    _stub_module(
        "homeassistant.helpers.aiohttp_client",
        async_get_clientsession=lambda *_args, **_kwargs: None,
    )
    _stub_module("homeassistant.util.dt", now=lambda: None, utcnow=lambda: None)

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

    class _EntityDevice(_Generic):
        pass

    class _Entity(_Generic):
        pass

    _stub_module(
        "custom_components.zendure_ha.binary_sensor", ZendureBinarySensor=_Entity
    )
    _stub_module("custom_components.zendure_ha.button", ZendureButton=_Entity)
    _stub_module(
        "custom_components.zendure_ha.entity",
        EntityDevice=_EntityDevice,
        EntityZendure=_Entity,
    )
    _stub_module("custom_components.zendure_ha.number", ZendureNumber=_Entity)
    _stub_module(
        "custom_components.zendure_ha.select",
        ZendureRestoreSelect=_Entity,
        ZendureSelect=_Entity,
    )
    _stub_module(
        "custom_components.zendure_ha.sensor",
        ZendureRestoreSensor=_Entity,
        ZendureSensor=_Entity,
    )

    device_spec = importlib.util.spec_from_file_location(
        "custom_components.zendure_ha.device",
        ROOT / "custom_components" / "zendure_ha" / "device.py",
    )
    assert device_spec is not None and device_spec.loader is not None
    device_module = importlib.util.module_from_spec(device_spec)
    sys.modules[device_spec.name] = device_module
    device_spec.loader.exec_module(device_module)
    return device_module


DEVICE_MODULE = _load_device_module()
ZendureZenSdk = DEVICE_MODULE.ZendureZenSdk


class _Value:
    def __init__(self, value: int = 0) -> None:
        self.value = value

    @property
    def asInt(self) -> int:
        return self.value


class _Entity:
    def __init__(self, property_name: str) -> None:
        self.propertyName = property_name
        self.translation_key = property_name
        self.name = property_name


class _Hass:
    @staticmethod
    def async_create_task(
        coro: Coroutine[object, object, None],
    ) -> asyncio.Task[None]:
        return asyncio.create_task(coro)


class _SimulatedCommandError(RuntimeError):
    """Transport failure used to verify command-lock release."""


def _device() -> ZendureZenSdk:
    """Create a command-capable instance without constructing HA entities."""
    device = object.__new__(ZendureZenSdk)
    device.name = "SolarFlow 2400 Pro"
    device._command_lock = asyncio.Lock()
    device._power_command_generation = 0
    device._power_off_requested = False
    device._power_off_task = None
    device._last_power_off_command = 0.0
    device._last_power_off_properties = None
    device._power_off_verify_delays = (0.0,)
    device.connection = _Value(2)
    device.limitInput = _Value()
    device.limitOutput = _Value()
    device.connectionStatus = _Value(12)
    device.homeInput = _Value()
    device.homeOutput = _Value()
    device.batteryInput = _Value()
    device.batteryOutput = _Value()
    # Unit tests drive reconciliation explicitly, avoiding background sleepers.
    device._schedule_power_off_verification = lambda _generation: None
    return device


def _command_recorder(
    commands: list[dict[str, object]],
) -> Callable[[dict[str, object]], Awaitable[bool]]:
    async def send(command: dict[str, object]) -> bool:
        commands.append(command)
        return True

    return send


class DevicePowerControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_off_then_two_zero_limits_keeps_one_canonical_off(
        self,
    ) -> None:
        """Reproduce the user's automation sequence without the AC-mode flip."""
        device = _device()
        commands: list[dict[str, object]] = []

        async def send(command: dict[str, object]) -> bool:
            commands.append(command)
            return True

        device._send_command_unlocked = send
        device.doCommand = send

        await device.power_off()
        await device.entityWrite(_Entity("outputLimit"), 0)
        await device.entityWrite(_Entity("inputLimit"), 0)

        self.assertEqual(
            [
                {
                    "properties": {
                        "smartMode": 0,
                        "acMode": 2,
                        "outputLimit": 0,
                        "inputLimit": 0,
                    }
                }
            ],
            commands,
        )

    async def test_every_zero_write_order_converges_to_canonical_off(self) -> None:
        for order in itertools.permutations(("manager", "output", "input")):
            device = _device()
            commands: list[dict[str, object]] = []
            device._send_command_unlocked = _command_recorder(commands)
            for source in order:
                if source == "manager":
                    await device.power_off()
                elif source == "output":
                    await device.entityWrite(_Entity("outputLimit"), 0)
                else:
                    await device.entityWrite(_Entity("inputLimit"), 0)

            self.assertEqual(1, len(commands), order)
            self.assertEqual(
                {
                    "smartMode": 0,
                    "acMode": 2,
                    "outputLimit": 0,
                    "inputLimit": 0,
                },
                commands[0]["properties"],
                order,
            )

    async def test_nonzero_limit_writes_keep_the_requested_direction(self) -> None:
        device = _device()
        commands: list[dict[str, object]] = []

        async def send(command: dict[str, object]) -> bool:
            commands.append(command)
            return True

        device._send_command_unlocked = send

        await device.entityWrite(_Entity("outputLimit"), 600)
        await device.entityWrite(_Entity("inputLimit"), 700)

        self.assertEqual(2, commands[0]["properties"]["acMode"])
        self.assertEqual(600, commands[0]["properties"]["outputLimit"])
        self.assertEqual(1, commands[1]["properties"]["acMode"])
        self.assertEqual(700, commands[1]["properties"]["inputLimit"])

    async def test_failed_off_command_is_retried_instead_of_coalesced(self) -> None:
        device = _device()
        commands: list[dict[str, object]] = []
        outcomes = iter((False, True))

        async def send(command: dict[str, object]) -> bool:
            commands.append(command)
            return next(outcomes)

        device._send_command_unlocked = send

        await device.power_off()
        await device.entityWrite(_Entity("outputLimit"), 0)

        self.assertEqual(2, len(commands))

    async def test_new_power_between_two_off_requests_prevents_coalescing(self) -> None:
        device = _device()
        commands: list[dict[str, object]] = []

        async def send(command: dict[str, object]) -> bool:
            commands.append(command)
            return True

        device._send_command_unlocked = send

        await device.power_off()
        await device.discharge(500)
        await device.power_off()

        self.assertEqual(3, len(commands))
        self.assertEqual(0, commands[-1]["properties"]["outputLimit"])
        self.assertEqual(0, commands[-1]["properties"]["smartMode"])

    async def test_stale_scheduler_cannot_replace_a_newer_off_verifier(self) -> None:
        device = _device()
        del device._schedule_power_off_verification
        device.hass = _Hass()
        device._power_off_verify_delays = (60.0,)

        async def send(_command: dict[str, object]) -> bool:
            return True

        device._send_command_unlocked = send
        properties = device._power_off_properties()
        old_generation = await device._write_power_command(properties, power_off=True)
        device._last_power_off_command = 0
        new_generation = await device._write_power_command(properties, power_off=True)

        device._schedule_power_off_verification(new_generation)
        new_task = device._power_off_task
        device._schedule_power_off_verification(old_generation)

        self.assertIs(new_task, device._power_off_task)
        device.cancel_pending_tasks()
        await asyncio.sleep(0)

    async def test_duplicate_off_keeps_existing_verification_window(self) -> None:
        device = _device()
        del device._schedule_power_off_verification
        device.hass = _Hass()
        device._power_off_verify_delays = (60.0,)

        async def send(_command: dict[str, object]) -> bool:
            return True

        device._send_command_unlocked = send
        await device.power_off()
        generation = device._power_command_generation
        verifier = device._power_off_task

        await device.entityWrite(_Entity("outputLimit"), 0)
        await device.entityWrite(_Entity("inputLimit"), 0)

        self.assertEqual(generation, device._power_command_generation)
        self.assertIs(verifier, device._power_off_task)
        device.cancel_pending_tasks()
        await asyncio.sleep(0)

    async def test_device_lock_makes_off_last_after_an_inflight_direct_write(
        self,
    ) -> None:
        device = _device()
        order: list[str] = []
        discharge_started = asyncio.Event()
        release_discharge = asyncio.Event()

        async def send(command: dict[str, object]) -> bool:
            properties = command["properties"]
            if properties["outputLimit"] == 500:
                order.append("discharge-start")
                discharge_started.set()
                await release_discharge.wait()
                order.append("discharge-complete")
            else:
                order.append("off")
            return True

        device._send_command_unlocked = send

        discharge_task = asyncio.create_task(device.discharge(500))
        await discharge_started.wait()
        off_task = asyncio.create_task(device.power_off())
        await asyncio.sleep(0)

        self.assertFalse(off_task.done())
        release_discharge.set()
        await asyncio.gather(discharge_task, off_task)

        self.assertEqual(["discharge-start", "discharge-complete", "off"], order)

    async def test_transport_exception_releases_device_command_lock(self) -> None:
        device = _device()

        async def fail(_command: dict[str, object]) -> bool:
            raise _SimulatedCommandError

        device._send_command_unlocked = fail
        with self.assertRaises(_SimulatedCommandError):
            await device.discharge(500)

        commands: list[dict[str, object]] = []

        async def succeed(command: dict[str, object]) -> bool:
            commands.append(command)
            return True

        device._send_command_unlocked = succeed
        await asyncio.wait_for(device.power_off(), timeout=0.2)

        self.assertEqual(1, len(commands))
        self.assertEqual(0, commands[0]["properties"]["outputLimit"])

    async def test_verifier_recovers_a_post_ack_power_reactivation(self) -> None:
        device = _device()
        commands: list[dict[str, object]] = []

        async def send(command: dict[str, object]) -> bool:
            commands.append(command)
            # The first stop is acknowledged but firmware later reactivates.
            # The retry converges the measured state to zero.
            if len(commands) == 2:
                device.batteryOutput.value = 0
            return True

        async def power_get() -> bool:
            return True

        device._send_command_unlocked = send
        device.power_get = power_get

        await device.power_off()
        generation = device._power_command_generation
        device.batteryOutput.value = 2100
        await device._verify_power_off(generation)

        self.assertEqual(2, len(commands))
        self.assertEqual(0, device.batteryOutput.asInt)

    async def test_verifier_keeps_watching_after_an_initial_zero_sample(self) -> None:
        device = _device()
        device._power_off_verify_delays = (0.0, 0.0)
        commands: list[dict[str, object]] = []
        reports = 0

        async def send(command: dict[str, object]) -> bool:
            commands.append(command)
            if len(commands) == 2:
                device.batteryOutput.value = 0
            return True

        async def power_get() -> bool:
            nonlocal reports
            reports += 1
            if reports == 2:
                device.batteryOutput.value = 2100
            return True

        device._send_command_unlocked = send
        device.power_get = power_get

        await device.power_off()
        generation = device._power_command_generation
        await device._verify_power_off(generation)

        self.assertGreaterEqual(reports, 2)
        self.assertEqual(2, len(commands))
        self.assertEqual(0, device.batteryOutput.asInt)

    async def test_stale_verifier_cannot_override_a_new_manual_command(self) -> None:
        device = _device()
        commands: list[dict[str, object]] = []

        async def send(command: dict[str, object]) -> bool:
            commands.append(command)
            return True

        async def power_get() -> bool:
            return True

        device._send_command_unlocked = send
        device.power_get = power_get

        await device.power_off()
        old_generation = device._power_command_generation
        await device.discharge(500)
        device.batteryOutput.value = 500
        await device._verify_power_off(old_generation)

        self.assertEqual(2, len(commands))
        self.assertEqual(500, commands[-1]["properties"]["outputLimit"])

    async def test_direct_ac_mode_write_is_serialized_and_invalidates_off(self) -> None:
        device = _device()
        commands: list[dict[str, object]] = []

        async def send(command: dict[str, object]) -> bool:
            commands.append(command)
            return True

        device._send_command_unlocked = send

        await device.power_off()
        off_generation = device._power_command_generation
        await device.entityWrite(_Entity("acMode"), 1)
        await device._verify_power_off(off_generation)

        self.assertEqual(2, len(commands))
        self.assertEqual({"acMode": 1}, commands[-1]["properties"])
        self.assertFalse(device._power_off_requested)

    async def test_offgrid_load_is_not_treated_as_failed_power_off(self) -> None:
        class _OffGridDevice(ZendureZenSdk):
            @property
            def pwr_offgrid(self) -> int:
                return 100

        device = object.__new__(_OffGridDevice)
        device.homeInput = _Value()
        device.homeOutput = _Value()
        device.batteryInput = _Value()
        device.batteryOutput = _Value(100)

        self.assertTrue(device._is_power_off())
        device.batteryOutput.value = 121
        self.assertFalse(device._is_power_off())

    async def test_simultaneous_pack_flows_use_net_battery_discharge(self) -> None:
        device = _device()
        device.batteryOutput.value = 500
        device.batteryInput.value = 500

        self.assertTrue(device._is_power_off())

        device.batteryOutput.value = 521
        self.assertFalse(device._is_power_off())

    async def test_unload_cancels_the_pending_verifier(self) -> None:
        device = _device()
        task = asyncio.create_task(asyncio.sleep(60))
        device._power_off_task = task

        device.cancel_pending_tasks()
        await asyncio.sleep(0)

        self.assertTrue(task.cancelled())
        self.assertIsNone(device._power_off_task)

    async def test_http_error_is_not_reported_as_a_successful_command(self) -> None:
        device = _device()

        class _Response:
            status = 500

            def __init__(self) -> None:
                self.released = False

            async def text(self) -> str:
                return "device rejected command"

            def release(self) -> None:
                self.released = True

        class _Session:
            def __init__(self) -> None:
                self.response = _Response()

            async def post(self, *_args: object, **_kwargs: object) -> _Response:
                return self.response

        device.session = _Session()
        device.httpid = 0
        device.snNumber = "test-sn"
        device.ipAddress = "test.local"

        result = await device.httpPost(
            "properties/write", {"properties": {"outputLimit": 0}}
        )

        self.assertFalse(result)
        self.assertTrue(device.session.response.released)

    async def test_missing_mqtt_transport_is_not_reported_as_success(self) -> None:
        device = _device()
        device.connection.value = 0
        device.mqtt = None

        result = await device._send_command_unlocked(
            {"properties": {"outputLimit": 0}}
        )

        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
