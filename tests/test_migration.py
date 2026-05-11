"""Regression test — async_migrate_entry advances entries to the current schema version."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.zendure_ha import async_migrate_entry
from custom_components.zendure_ha.config_flow import ZendureConfigFlow


def _make_entry(minor_version: int):
    entry = MagicMock()
    entry.version = 1
    entry.minor_version = minor_version
    entry.entry_id = "test-entry"
    return entry


class TestAsyncMigrateEntry:
    @pytest.mark.asyncio
    async def test_old_entry_migrated_to_current_minor_version(self):
        """An entry at minor_version < 5 is upgraded to the config-flow MINOR_VERSION."""
        hass = MagicMock()
        hass.config_entries.async_update_entry = MagicMock()
        entry = _make_entry(minor_version=3)

        with patch(
            "custom_components.zendure_ha.Migration.async_migrate", AsyncMock()
        ):
            result = await async_migrate_entry(hass, entry)

        assert result is True
        hass.config_entries.async_update_entry.assert_called_once_with(
            entry, version=1, minor_version=ZendureConfigFlow.MINOR_VERSION
        )

    @pytest.mark.asyncio
    async def test_entry_at_5_upgraded_to_current_minor_version(self):
        """An entry already at minor_version=5 is still bumped to the current version."""
        hass = MagicMock()
        hass.config_entries.async_update_entry = MagicMock()
        entry = _make_entry(minor_version=5)

        result = await async_migrate_entry(hass, entry)

        assert result is True
        hass.config_entries.async_update_entry.assert_called_once_with(
            entry, version=1, minor_version=ZendureConfigFlow.MINOR_VERSION
        )

    @pytest.mark.asyncio
    async def test_current_entry_still_succeeds(self):
        """An entry already at the current version passes through without error."""
        hass = MagicMock()
        hass.config_entries.async_update_entry = MagicMock()
        entry = _make_entry(minor_version=ZendureConfigFlow.MINOR_VERSION)

        result = await async_migrate_entry(hass, entry)

        assert result is True
        hass.config_entries.async_update_entry.assert_called_once_with(
            entry, version=1, minor_version=ZendureConfigFlow.MINOR_VERSION
        )
