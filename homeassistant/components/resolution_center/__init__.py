"""The resolution center integration."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from . import websocket_api
from .const import DOMAIN
from .issue_registry import async_load as async_load_issue_registry
from .resolution_center import async_create_issue, async_delete_issue  # noqa: F401


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Resolution Center."""
    hass.data[DOMAIN] = {"issues": {}}

    websocket_api.async_setup(hass)
    await async_load_issue_registry(hass)

    return True
