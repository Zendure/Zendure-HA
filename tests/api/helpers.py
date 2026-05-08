from __future__ import annotations

import pytest
from pytest_mock import MockerFixture

PROPERTIES_REPORT = {
    "timestamp": 1778150268,
    "sn": "EOD1NLN9P010318",
    "version": 2,
    "product": "solarFlow800Pro2",
    "properties": {
        "electricLevel": 71,
        "outputPackPower": 285,
        "packInputPower": 0,
        "gridInputPower": 285,
        "solarInputPower": 0,
        "inverseMaxPower": 800,
        "acMode": 1,
        "smartMode": 1,
    },
}

CLOUD_EMPTY = {
    "code": 200,
    "success": True,
    "data": {"mqtt": {}, "deviceList": []},
    "msg": "Operation successful",
}

CLOUD_WITH_HYPER = {
    "code": 200,
    "success": True,
    "data": {
        "mqtt": {"clientId": "c1", "url": "mqtt.zendure.tech:1883", "username": "u", "password": "p"},
        "deviceList": [
            {"deviceKey": "HYP001", "productModel": "hyper2000", "deviceName": "Hyper 2000",
             "snNumber": "HYP001", "ip": ""},
        ],
    },
    "msg": "Operation successful",
}


def _mock_http_response(mocker: MockerFixture, payload: dict) -> object:
    """Async mock for `await session.get/post(...)` — returns response directly."""
    resp = mocker.MagicMock()
    resp.json = mocker.AsyncMock(return_value=payload)
    resp.status = 200
    return mocker.AsyncMock(return_value=resp)


@pytest.fixture()
def hass(mocker: MockerFixture) -> object:
    return mocker.MagicMock()
