"""Monsieur Cuisine (inoffiziell) – Rezepte aus Home Assistant aufs Gerät.

Inoffizielle Integration, keine Verbindung zu Lidl, SilverCrest oder Tecpal.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from . import llm_api, services
from .const import (
    CONF_COMPANION_APP_ID,
    CONF_JWT,
    CONF_LOCALE,
    CONF_USER_ID,
    DATA_ACCOUNT,
    DATA_LIVE,
    DOMAIN,
)
from .coordinator import McAccountCoordinator, McCoordinator, McLiveCoordinator
from .mcbridge.client import JwtAuth, McClient
from .mcbridge.locales import DEFAULT_LOCALE
from .migration import CURRENT_VERSION, migrate_data

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.BINARY_SENSOR, Platform.CALENDAR, Platform.SENSOR, Platform.TODO]

# Keine YAML-Konfiguration – Einrichtung nur über die Oberfläche (Config-Entries).
# Macht hassfest zufrieden (async_setup ohne eigenes Schema).
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Aktionen und Werkzeuge für Sprachmodelle – unabhängig vom einzelnen Konto."""
    hass.data.setdefault(DOMAIN, {})
    services.async_register(hass)
    llm_api.async_register(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Config-Eintrag auf das aktuelle Format heben.

    HA ruft das bei jedem Versionssprung des Eintrags auf – entscheidend fürs
    Update: Ein neuer Code darf das gespeicherte Format ändern, ohne dass sich
    der Nutzer neu anmelden muss. Die Anmeldung (Tokens) bleibt erhalten; nur
    die Felder ziehen um (siehe ``migration.migrate_data``).
    """
    _LOGGER.debug("MC-Migration: Eintrag Version %s", entry.version)
    if entry.version > CURRENT_VERSION:
        # Neuerer Eintrag als dieser Code (Herabstufung) – nicht migrierbar.
        _LOGGER.warning(
            "MC-Migration: Eintrag Version %s ist neuer als unterstützt (%s)",
            entry.version, CURRENT_VERSION,
        )
        return False
    if entry.version < CURRENT_VERSION:
        neu = migrate_data(entry.version, dict(entry.data))
        hass.config_entries.async_update_entry(entry, data=neu, version=CURRENT_VERSION)
        _LOGGER.info("MC-Migration: Eintrag auf Version %s gehoben", CURRENT_VERSION)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)

    def _save_jwt(jwt: str) -> None:
        # Der JWT wird ~stündlich erneuert → neuen Wert sofort speichern
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_JWT: jwt})

    auth = JwtAuth(
        session,
        entry.data[CONF_JWT],
        entry.data[CONF_COMPANION_APP_ID],
        on_refresh=_save_jwt,
        user_id=entry.data.get(CONF_USER_ID),
    )
    # Die Sprache filtert auch Einkaufsliste und Planer: falsche Sprache = leer.
    client = McClient(session, auth, entry.data.get(CONF_LOCALE, DEFAULT_LOCALE))
    coordinator = McCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    # Konto-Daten (Einkaufsliste, Wochenplaner). Dürfen die Einrichtung nicht
    # aufhalten: ein Fehler lässt nur todo/calendar vorerst leer, der Rest läuft.
    account = McAccountCoordinator(hass, entry, client)
    await account.async_refresh()

    # Live-Status (Push über gRPC). Läuft neben dem regelmäßigen Abruf und
    # darf die Einrichtung nicht aufhalten: schlägt der Strom fehl, bleiben nur
    # die Live-Entitäten unbestimmt.
    live = McLiveCoordinator(hass, entry, auth, client)
    live.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client, "coordinator": coordinator,
        DATA_ACCOUNT: account, DATA_LIVE: live,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        daten = hass.data[DOMAIN].pop(entry.entry_id, None) or {}
        if live := daten.get(DATA_LIVE):
            await live.async_stop()
    return ok
