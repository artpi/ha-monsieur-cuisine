"""Locale-specific choices of the recipe editor (categories, units, complexity).

``options.json`` is derived from monsieur-cuisine-cli's recipe validation data
(MIT, Copyright (c) 2024 Filipe Pina). The IDs differ per locale, so a recipe
must use the IDs of its own ``languageLocale``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_LOCALE = "de-DE"

OPTIONS: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).parent / "options.json").read_text(encoding="utf-8")
)
LOCALES = sorted(OPTIONS)

# Texts the integration itself puts into a recipe.
_WORDS = {
    "de": {"step": "Schritt", "ingredients": "Zutaten", "servings": "Portionen"},
    "pl": {"step": "Krok", "ingredients": "Składniki", "servings": "porcje"},
    "en": {"step": "Step", "ingredients": "Ingredients", "servings": "servings"},
}


def word(locale: str, key: str) -> str:
    return _WORDS.get(locale.split("-")[0], _WORDS["en"])[key]


def options(locale: str) -> dict[str, Any]:
    return OPTIONS.get(locale) or OPTIONS[DEFAULT_LOCALE]


def default_complexity(locale: str) -> int:
    """ID of the middle of the three levels."""
    return options(locale)["complexity"][1]["id"]


def serving_unit(locale: str, unit: str | None) -> str:
    """``unit`` if the editor offers it in this locale, else a sensible default."""
    units = options(locale)["serving_units"]
    if unit in units:
        return unit
    default = word(locale, "servings")
    return default if default in units else units[0]


def locale_for(language: str | None, country: str | None) -> str:
    """Best supported locale for Home Assistant's language and country."""
    wanted = f"{(language or '').split('-')[0].lower()}-{(country or '').upper()}"
    if wanted in OPTIONS:
        return wanted
    for loc in LOCALES:
        if country and loc.endswith(f"-{country.upper()}"):
            return loc
    for loc in LOCALES:
        if language and loc.startswith(f"{language.split('-')[0].lower()}-"):
            return loc
    return DEFAULT_LOCALE
