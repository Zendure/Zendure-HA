"""Tests for battery sub-device registration from packData."""
from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest
from pytest_mock import MockerFixture

from tests.device.helpers import _patch_entity_add


def _make_device(mocker: MockerFixture) -> object:
    """Build a minimal ZendureZenSdk stub for mqttProperties tests."""
    from custom_components.zendure_ha.device import ZendureZenSdk

    _patch_entity_add(mocker)

    device = mocker.MagicMock()
    device.hass = mocker.MagicMock()
    device.batteries = {}
    device.lastseen = MagicMock()
    device.socStatus = mocker.MagicMock()
    device.socStatus.asInt = 0
    device.hemsState = mocker.MagicMock()
    device.hemsState.is_on = False
    device.fuseGroup = mocker.MagicMock()
    device.fuseGroup.value = 1
    device.connection = mocker.MagicMock()
    device.connection.value = 2
    device.online = True
    device.setStatus = mocker.MagicMock()
    device.kWh = 0.0
    device.minSoc = mocker.MagicMock()
    device.minSoc.asNumber = 0
    device.electricLevel = mocker.MagicMock()
    device.electricLevel.asNumber = 50
    device.totalKwh = mocker.MagicMock()
    device.availableKwh = mocker.MagicMock()

    device.mqttProperties = types.MethodType(ZendureZenSdk.mqttProperties, device)
    return device


_PACK_DATA = [{"sn": "COD1TEST0001", "electricLevel": 75, "totalVol": 5100}]
_BATTERY_PATH = "custom_components.zendure_ha.device.ZendureBattery"


class TestBatteryRegistration:
    """packData must register and populate battery entities in one shot."""

    @pytest.mark.asyncio
    async def test_new_battery_added_to_dict(self, mocker: MockerFixture) -> None:
        """First packData message must create the battery in device.batteries."""
        device = _make_device(mocker)
        fake_bat = mocker.MagicMock()
        with patch(_BATTERY_PATH, return_value=fake_bat):
            await device.mqttProperties({"packData": _PACK_DATA})
        assert "COD1TEST0001" in device.batteries

    @pytest.mark.asyncio
    async def test_new_battery_properties_applied_immediately(self, mocker: MockerFixture) -> None:
        """Battery entityUpdate must be called on the first packData — not only after the second."""
        device = _make_device(mocker)
        fake_bat = mocker.MagicMock()
        with patch(_BATTERY_PATH, return_value=fake_bat):
            await device.mqttProperties({"packData": _PACK_DATA})

        called_keys = {call.args[0] for call in fake_bat.entityUpdate.call_args_list}
        assert "electricLevel" in called_keys
        assert "totalVol" in called_keys

    @pytest.mark.asyncio
    async def test_sn_key_excluded_from_entity_update(self, mocker: MockerFixture) -> None:
        """'sn' must never be passed to entityUpdate — it is only used for lookup."""
        device = _make_device(mocker)
        fake_bat = mocker.MagicMock()
        with patch(_BATTERY_PATH, return_value=fake_bat):
            await device.mqttProperties({"packData": _PACK_DATA})

        called_keys = {call.args[0] for call in fake_bat.entityUpdate.call_args_list}
        assert "sn" not in called_keys

    @pytest.mark.asyncio
    async def test_second_packdata_updates_existing_battery(self, mocker: MockerFixture) -> None:
        """A second packData message must update the existing battery, not create a duplicate."""
        device = _make_device(mocker)
        fake_bat = mocker.MagicMock()
        with patch(_BATTERY_PATH, return_value=fake_bat):
            await device.mqttProperties({"packData": _PACK_DATA})
            await device.mqttProperties({"packData": [{"sn": "COD1TEST0001", "electricLevel": 80}]})

        assert len(device.batteries) == 1
        # entityUpdate called at least twice (once per packData message)
        assert fake_bat.entityUpdate.call_count >= 2

    @pytest.mark.asyncio
    async def test_entry_without_sn_is_skipped(self, mocker: MockerFixture) -> None:
        """A packData entry without an 'sn' key must be silently skipped."""
        device = _make_device(mocker)
        with patch(_BATTERY_PATH) as bat_cls:
            await device.mqttProperties({"packData": [{"electricLevel": 50}]})
        bat_cls.assert_not_called()
        assert len(device.batteries) == 0


class TestBatteryRegistrationRegression:
    """Regression: the old elif-branch skipped entityUpdate on the first packData message.

    These tests document the exact failure mode so the bug cannot silently regress.
    The old code read:

        if bat is None:
            self.batteries[sn] = ZendureBattery(...)
        elif bat and b:          # ← skipped for newly created battery
            for key, value in b.items(): ...

    A battery created in the `if` branch would never reach the `elif`, so its
    entities (electricLevel, totalVol, …) were never created during the first
    packData message.  The battery appeared in device.batteries but was invisible
    in Home Assistant until a *second* packData arrived (up to 60 s later).
    """

    @pytest.mark.asyncio
    async def test_old_elif_logic_would_miss_first_properties(self, mocker: MockerFixture) -> None:
        """Reproduce the old elif-only flow to confirm it did NOT call entityUpdate."""
        fake_bat = mocker.MagicMock()
        batteries: dict = {}

        # Replicate the old (buggy) logic verbatim
        b = {"sn": "COD1TEST0001", "electricLevel": 75}
        sn = b["sn"]
        if (bat := batteries.get(sn, None)) is None:
            batteries[sn] = fake_bat
        elif bat and b:
            for key, value in b.items():
                if key != "sn":
                    fake_bat.entityUpdate(key, value)

        # The old code never reached entityUpdate on first creation
        fake_bat.entityUpdate.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_if_logic_calls_properties_on_first_creation(self, mocker: MockerFixture) -> None:
        """The fixed if-branch calls entityUpdate even for newly created batteries."""
        fake_bat = mocker.MagicMock()
        batteries: dict = {}

        # Fixed logic
        b = {"sn": "COD1TEST0001", "electricLevel": 75}
        sn = b["sn"]
        if (bat := batteries.get(sn, None)) is None:
            bat = fake_bat
            batteries[sn] = bat
        if bat and b:
            for key, value in b.items():
                if key != "sn":
                    bat.entityUpdate(key, value)

        fake_bat.entityUpdate.assert_called_once_with("electricLevel", 75)
