"""Builders shared by the tests."""

from __future__ import annotations

from types import MappingProxyType, SimpleNamespace
from typing import Any


def subentry(data: dict[str, Any], title: str, subentry_type: str = "zendure") -> dict[str, Any]:
    return {"data": data, "subentry_type": subentry_type, "title": title, "unique_id": f"{subentry_type}_{data.get('sn', title)}"}


def ble_info(name: str, address: str, sn_tail: bytes | None = None) -> SimpleNamespace:
    # the parts of a BluetoothServiceInfoBleak the driver reads
    return SimpleNamespace(name=name, address=address, manufacturer_data={1: sn_tail + b"\x00"} if sn_tail else {})


def frozen(data: dict[str, Any]) -> MappingProxyType:
    return MappingProxyType(data)
