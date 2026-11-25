"""The actions as Home Assistant services with response data."""
from __future__ import annotations

from functools import partial
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback

from .actions import ACTIONS, Action, async_run
from .const import DOMAIN


async def _handle(action: Action, call: ServiceCall) -> dict[str, Any]:
    return await async_run(call.hass, action, dict(call.data))


@callback
def async_register(hass: HomeAssistant) -> None:
    for action in ACTIONS:
        hass.services.async_register(
            DOMAIN, action.name, partial(_handle, action),
            schema=vol.Schema({**action.schema, **(action.admin_schema or {})}),
            supports_response=SupportsResponse.ONLY if action.read_only else SupportsResponse.OPTIONAL,
        )
