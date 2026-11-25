"""Write format of own recipes, locales and compact views."""
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1] / "custom_components" / "monsieur_cuisine"))

from mcbridge import build_payload, from_schema_org  # noqa: E402
from mcbridge.authoring import (  # noqa: E402
    apply_fields,
    new_payload,
    recipe_details,
    write_payload,
)
from mcbridge.locales import locale_for, options  # noqa: E402

FIELDS = {
    "title": "Zupa", "servings": 4, "preparation_minutes": 10, "ready_minutes": 30, "calories": 100,
    "ingredients": [{"name": "woda", "amount": "500", "unit": "ml"},
                    {"name": "sól", "amount": "1", "unit": "szczypta"},
                    {"name": "śmietana", "group": "Sos"}],
    "steps": [{"text": "Zagotuj", "program": "customized", "temperature": 100, "speed": 1,
               "time_seconds": 600, "reverse": True},
              {"text": "Podaj", "title": "Podanie"}],
}


def test_new_recipe_is_a_polish_draft():
    p = new_payload(FIELDS, "pl-PL")
    assert (p["status"], p["languageLocale"], p["complexityId"]) == ("draft", "pl-PL", 102)
    size = p["servingSizes"][0]
    assert (size["amount"], size["unit"]) == (4, "porcje")
    assert [g["name"] for g in size["ingredientGroups"]] == ["Składniki", "Sos"]
    assert size["ingredientGroups"][0]["ingredients"][0]["name"] == "woda (ml)"
    assert size["ingredientGroups"][0]["ingredients"][1]["unit"] == "szczypta"
    assert size["steps"][0]["title"] == "Krok 1"
    assert size["steps"][0]["mode"]["deviceSettings"] == [
        {"temperature": 100, "speed": 1, "time": 600, "order": 0, "clockwise": False}]
    assert "mode" not in size["steps"][1]


@pytest.mark.parametrize("step", [
    {"text": "x", "speed": 3},
    {"text": "x", "program": "kneading", "speed": 3},
    {"text": "x", "program": "scale"},
])
def test_invalid_steps(step):
    with pytest.raises(ValueError, match="Step 1"):
        new_payload({"title": "x", "steps": [step]}, "pl-PL")


def test_ids_must_belong_to_the_locale():
    german = options("de-DE")["categories"][0]["id"]
    with pytest.raises(ValueError):
        new_payload({"title": "x", "category_ids": [german]}, "pl-PL")


API_RECIPE = {
    "id": 7, "status": "private-publish", "source": "member", "languageLocale": "pl-PL", "title": "Zupa",
    "complexity": {"id": 102, "name": "średnio trudne"}, "deviceTypes": [{"id": 13, "code": "MC3.0"}],
    "categories": [{"id": 230, "name": "Zupy"}],
    "nutrients": [{"name": "fat", "unit": "g", "amount": 3, "amountFloat": 3.0}],
    "detailsImage": {"landscapeMedia": {"id": 5, "url": "https://x/y.jpg", "isAiGenerated": True},
                     "portraitMedia": None},
    "servingSizes": [{"id": 1, "amount": 2, "unit": "porcje", "isDefault": True, "preparationTime": 5,
                      "readyInTime": 9, "maxServing": None,
                      "ingredientGroups": [{"id": 3, "name": "Składniki", "isDefault": True, "order": 0,
                                            "ingredients": [{"id": 4, "name": "woda", "amount": "1", "unit": "",
                                                             "order": 0, "systemIngredientId": 9}]}],
                      "steps": [{"id": 8, "title": "Krok 1", "description": "Gotuj", "duration": None,
                                 "mode": {"type": "turbo", "modules": None, "modeSetting": None,
                                          "deviceSettings": [{"time": 5, "order": 0}]}}]}],
}


def test_update_keeps_everything_else():
    payload = apply_fields(write_payload(API_RECIPE), {"description": "nowy", "fat": 4}, "pl-PL")
    assert payload["status"] == "private-publish"
    assert payload["description"] == "nowy"
    assert payload["categoryIds"] == [230] and payload["complexityId"] == 102
    assert payload["nutrients"] == [{"name": "fat", "unit": "g", "amount": 4}]
    assert payload["detailsImage"] == {"portraitMediaId": None, "portraitIsAiGenerated": False,
                                       "landscapeMediaId": 5, "landscapeIsAiGenerated": True}
    size = payload["servingSizes"][0]
    assert size["steps"][0]["mode"] == {"type": "turbo", "modeSetting": None,
                                        "deviceSettings": [{"time": 5, "order": 0}]}
    assert size["ingredientGroups"][0]["ingredients"][0]["systemIngredientId"] == 9
    assert not {"id", "maxServing"} & set(size)


def test_details_of_own_and_catalogue_recipes():
    own = recipe_details(API_RECIPE)["serving_sizes"][0]
    assert own["steps"] == [{"title": "Krok 1", "text": "Gotuj", "program": "turbo", "time_seconds": 5}]
    public = recipe_details({"id": 1, "name": "A", "servingSizes": [{
        "amount": 4, "servingUnit": "porcje", "ingredientGroups": [{"id": 2, "name": "Sos"}],
        "ingredients": [{"name": "sól", "amount": "1", "unit": "g", "ingredientGroupId": 2}],
        "steps": [{"name": "S", "description": "T", "deviceSetting": {
            "mode": "customized", "reverse": True, "settings": [{"speed": 2, "time": 60}]}}]}]})
    size = public["serving_sizes"][0]
    assert size["ingredients"] == [{"group": "Sos", "amount": "1", "unit": "g", "name": "sól"}]
    assert size["steps"][0] == {"title": "S", "text": "T", "program": "customized", "speed": 2,
                                "time_seconds": 60, "reverse": True}


def test_imported_recipe_uses_the_locale():
    r = from_schema_org({"name": "Zupa", "recipeYield": "4", "image": [{"url": "https://x/a.jpg"}],
                         "recipeIngredient": ["2 EL Öl"],
                         "recipeInstructions": ["Gotuj 10 min/100°C/obr. 1."]}, "pl-PL")
    p = build_payload(r, locale="pl-PL")
    assert p["languageLocale"] == "pl-PL" and p["complexityId"] == 102
    size = p["servingSizes"][0]
    assert size["unit"] == "porcje" and size["ingredientGroups"][0]["name"] == "Składniki"
    assert size["ingredientGroups"][0]["ingredients"][0]["unit"] == ""
    assert size["steps"][0]["mode"]["deviceSettings"][0]["time"] == 600
    assert r.image_url == "https://x/a.jpg"


def test_locale_for():
    assert locale_for("pl", "PL") == "pl-PL"
    assert locale_for("en", "PL") == "pl-PL"
    assert locale_for("en-GB", None) == "en-US"
    assert locale_for(None, None) == "de-DE"
