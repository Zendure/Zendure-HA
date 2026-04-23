"""Zendure cloud authentication."""

from __future__ import annotations

import hashlib
import logging
import secrets
import traceback
from base64 import b64decode
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_APPTOKEN, CONF_HAKEY, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def api_ha(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any] | None:
    """Authenticate with Zendure cloud API and retrieve device list."""
    session = async_get_clientsession(hass)

    if (token := data.get(CONF_APPTOKEN)) is not None and len(token) > 1:
        base64_url = b64decode(str(token)).decode("utf-8")
        api_url, appKey = base64_url.rsplit(".", 1)
    else:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_zendure_token")

    try:
        body = {
            "appKey": appKey,
        }

        # Prepare signature parameters
        timestamp = int(datetime.now().timestamp())
        nonce = str(secrets.randbelow(90000) + 10000)

        # Merge all parameters to be signed and sort by key in ascending order
        sign_params = {
            **body,
            "timestamp": timestamp,
            "nonce": nonce,
        }

        # Construct signature string
        body_str = "".join(f"{k}{v}" for k, v in sorted(sign_params.items()))

        # Calculate signature
        sign_str = f"{CONF_HAKEY}{body_str}{CONF_HAKEY}"
        sha1 = hashlib.sha1()  # noqa: S324
        sha1.update(sign_str.encode("utf-8"))
        sign = sha1.hexdigest().upper()

        # Build request headers
        headers = {
            "Content-Type": "application/json",
            "timestamp": str(timestamp),
            "nonce": nonce,
            "clientid": "zenHa",
            "sign": sign,
        }

        result = await session.post(url=f"{api_url}/api/ha/deviceList", json=body, headers=headers)
        response_data = await result.json()

        if response_data.get("code") != 200:
            _LOGGER.error("Zendure API error %s: %s", response_data.get("code"), response_data.get("msg"))
            return None

        if len(response_data["data"]["deviceList"]) == 0:
            _LOGGER.error("Zendure API does not reply any devices: %s", response_data)
            return None

        if len(response_data["data"]["mqtt"]) == 0:
            _LOGGER.error("Zendure API does not reply any mqtt info: %s", response_data)
            return None

        if not response_data.get("success", False) or (result := response_data["data"]) is None:
            return None

        return dict(result)

    except Exception as e:
        _LOGGER.error("Unable to connect to Zendure %s!", e)
        _LOGGER.error(traceback.format_exc())
        return None
