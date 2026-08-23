"""Tests for ZendureRestoreSelect restoring a state whose option appears later.

The fuseGroup select is created with a fixed set of options and only gains its
"Part of <device> fusegroup" entries once update_fusegroups() has seen every
device (manager.py). async_added_to_hass therefore runs *before* those options
exist, and a restored value pointing at one of them used to be dropped on the
floor, silently resetting the user's choice to "unused" on every reload.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.zendure_ha.select import ZendureRestoreSelect

FUSEGROUPS: dict[int, str] = {0: "unused", 1: "owncircuit", 2: "group800"}
DYNAMIC = "Part of Hyper 2000 fusegroup"


class _FakeState:
    def __init__(self, state: str) -> None:
        self.state = state


def _make_select(hass, device, restored: str | None) -> ZendureRestoreSelect:  # noqa: ANN001
    select = ZendureRestoreSelect(device, "fuseGroup", dict(FUSEGROUPS), None)
    select.hass = hass
    select.entity_id = "select.test_fusegroup"
    # not attached to a platform: keep state writes out of the way
    select.async_write_ha_state = lambda: None  # type: ignore[method-assign]

    async def _last_state() -> _FakeState | None:
        return None if restored is None else _FakeState(restored)

    select.async_get_last_state = _last_state  # type: ignore[method-assign]
    return select


@pytest.fixture
def added_select(hass, zensdk_device):  # noqa: ANN001, ANN201
    """Build a select that has already been through async_added_to_hass."""

    async def _build(restored: str | None) -> ZendureRestoreSelect:
        select = _make_select(hass, zensdk_device, restored)
        await select.async_added_to_hass()
        return select

    return _build


async def test_restores_option_that_exists_at_add_time(added_select) -> None:  # noqa: ANN001
    select = await added_select("owncircuit")

    assert select.current_option == "owncircuit"


async def test_falls_back_to_first_option_without_stored_state(added_select) -> None:  # noqa: ANN001
    select = await added_select(None)

    assert select.current_option == "unused"


async def test_dynamic_option_added_later_is_restored(added_select) -> None:  # noqa: ANN001
    select = await added_select(DYNAMIC)

    # the dynamic option does not exist yet, so the select shows the fallback
    assert select.current_option == "unused"

    # update_fusegroups() extends the options once every device is known
    extended: dict[Any, str] = dict(FUSEGROUPS)
    extended["dev-1"] = DYNAMIC
    select.setDict(extended)

    assert select.current_option == DYNAMIC


async def test_pending_restore_applies_only_once(added_select) -> None:  # noqa: ANN001
    select = await added_select(DYNAMIC)
    extended: dict[Any, str] = dict(FUSEGROUPS)
    extended["dev-1"] = DYNAMIC
    select.setDict(extended)
    await select.async_select_option("group800")

    # a later refresh of the same options must not resurrect the restored value
    select.setDict(extended)

    assert select.current_option == "group800"


async def test_explicit_choice_cancels_a_still_pending_restore(added_select) -> None:  # noqa: ANN001
    select = await added_select(DYNAMIC)
    await select.async_select_option("group800")

    extended: dict[Any, str] = dict(FUSEGROUPS)
    extended["dev-1"] = DYNAMIC
    select.setDict(extended)

    assert select.current_option == "group800"
