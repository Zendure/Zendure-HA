"""Module for the Zendure P1 meter integration in Home Assistant."""

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from aiohttp import ClientTimeout
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from custom_components.zendure_ha.entity import EntityDevice

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=2)
HTTP_TIMEOUT = ClientTimeout(total=2)
POWER_KEYS = ("total_power", "a_aprt_power", "b_aprt_power", "c_aprt_power")


class P1Meter(EntityDevice):
    """Zendure P1 meter: reports the grid power over the local zenSDK http api.

    It is a sensor only, so it is not part of the battery dispatching. Select its
    `total_power` entity as the P1 meter of the integration to steer on it.
    """

    @staticmethod
    def isP1Meter(productModel: str) -> bool:
        return "p1" in productModel.lower().replace(" ", "")

    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise the P1 meter."""
        sn = definition.get("snNumber") or deviceId
        super().__init__(hass, deviceId, prodName, definition["productModel"], definition.get("productKey", ""), sn)
        self.session = async_get_clientsession(hass, verify_ssl=False)
        self.ipAddress = definition.get("ip") or f"zendure-{definition['productModel'].replace(' ', '')}-{sn}.local"
        self.lastseen = datetime.min
        self._unsub: Callable[[], None] | None = None

    def start(self) -> None:
        self._unsub = async_track_time_interval(self.hass, self._poll, POLL_INTERVAL)

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _poll(self, _now: datetime) -> None:
        try:
            response = await self.session.get(f"http://{self.ipAddress}/properties/report", timeout=HTTP_TIMEOUT)
            payload = json.loads(await response.text())
        except Exception as e:
            if self.lastseen != datetime.min:
                _LOGGER.error("%s for %s during P1 poll%s", type(e).__name__, self.name, f": {e}" if str(e) else "!")
            self.lastseen = datetime.min
            return

        self.lastseen = datetime.now()
        for key in POWER_KEYS:
            if (value := payload.get(key)) is not None:
                self.entityUpdate(key, value)
