"""Setup through the UI: choose a locale, sign in with Lidl, paste the callback.

Lidl hands the one-time code only to an address compiled into the vendor's app
(``lidl-com…://login/prod``). No desktop browser can open it, but Chrome prints
it in the developer console, from where the user pastes it here. Unlike the
upstream browser helper this needs no extension and no unauthenticated
callback endpoint in Home Assistant.

Authorization Code with PKCE; the code is redeemed exactly once, when the form
is submitted.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    CONF_COMPANION_APP_ID,
    CONF_JWT,
    CONF_LOCALE,
    CONF_USER_ID,
    DOMAIN,
)
from .mcbridge.auth import (
    CallbackError,
    McAuthError,
    PkcePair,
    authorize_url,
    login_with_code,
    new_pkce,
    parse_callback,
)
from .mcbridge.locales import DEFAULT_LOCALE, LOCALES, locale_for

_LOGGER = logging.getLogger(__name__)

CONF_CALLBACK = "callback"


class McConfigFlow(ConfigFlow, domain=DOMAIN):
    # Muss mit migration.CURRENT_VERSION übereinstimmen. Regel: nie anheben,
    # ohne in migration.migrate_data einen Umzug (und einen Test) zu ergänzen –
    # sonst erzwingt ein Update eine neue Anmeldung.
    VERSION = 2

    def __init__(self) -> None:
        self._pkce: PkcePair = new_pkce()
        self._locale = DEFAULT_LOCALE

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._locale = user_input[CONF_LOCALE]
            return await self.async_step_login()
        default = locale_for(self.hass.config.language, self.hass.config.country)
        return self.async_show_form(step_id="user", data_schema=vol.Schema({
            vol.Required(CONF_LOCALE, default=default): SelectSelector(
                SelectSelectorConfig(options=LOCALES, mode=SelectSelectorMode.DROPDOWN)),
        }))

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """The stored session is no longer valid; refresh the existing entry."""
        self._locale = entry_data.get(CONF_LOCALE, DEFAULT_LOCALE)
        return await self.async_step_login()

    async def async_step_login(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                code = parse_callback(user_input[CONF_CALLBACK], self._pkce.state)
                result = await login_with_code(
                    async_get_clientsession(self.hass), code, self._pkce, self._locale)
            except CallbackError as err:
                errors["base"] = str(err)
            except McAuthError as err:
                _LOGGER.warning("Monsieur Cuisine sign-in failed: %s", err)
                errors["base"] = "invalid_auth"
                # The code is spent: the next attempt needs a new sign-in page.
                self._pkce = new_pkce()
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.warning("Monsieur Cuisine sign-in failed: %s", err)
                errors["base"] = "cannot_connect"
                self._pkce = new_pkce()
            else:
                return await self._complete(result)
        return self.async_show_form(
            step_id="login",
            data_schema=vol.Schema({
                vol.Required(CONF_CALLBACK): TextSelector(TextSelectorConfig(multiline=True)),
            }),
            description_placeholders={"login_url": authorize_url(self._pkce, self._locale)},
            errors=errors,
        )

    async def _complete(self, result: dict[str, Any]) -> ConfigFlowResult:
        user = result["user"]
        await self.async_set_unique_id(str(user["id"]))
        data = {
            CONF_JWT: result["jwt"],
            CONF_USER_ID: user["id"],
            CONF_COMPANION_APP_ID: result["companion_app_id"],
            CONF_LOCALE: self._locale,
        }
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="reauth_account_mismatch")
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=data)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=user.get("displayName") or "Monsieur Cuisine", data=data)
