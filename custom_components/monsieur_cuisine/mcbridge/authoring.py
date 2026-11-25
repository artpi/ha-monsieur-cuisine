"""Building and reading the write format of own recipes.

Programs and their parameters are those of the Monsieur Cuisine Smart (MC3.0);
the API validates the numeric ranges. The program catalogue was cross-checked
with Unofficial-Monsieur-Cuisine-MCP (Apache-2.0) and the list of response-only
fields with monsieur-cuisine-cli (MIT).
"""
from __future__ import annotations

from html import unescape
from typing import Any

from .locales import default_complexity, options, serving_unit, word
from .models import DEVICE_MC_SMART

STATUS_DRAFT = "draft"
STATUS_PRIVATE = "private-publish"
# Every status of an own recipe except the trash.
OWN_STATUSES = ("draft", "pending", "private-publish", "publish", "rejected")

# program -> allowed step settings
PROGRAMS: dict[str, tuple[str, ...]] = {
    "customized": ("temperature", "speed", "time_seconds", "reverse"),
    "kneading": ("time_seconds",),
    "kneading_solid_dough": ("time_seconds",),
    "kneading_soft_dough": ("time_seconds",),
    "kneading_liquid_dough": ("time_seconds",),
    "steaming": ("time_seconds",),
    "roasting": ("temperature", "time_seconds"),
    "scale": ("weight_grams",),
    "sous_vide": ("temperature", "time_seconds"),
    "slow_cooking": ("temperature", "time_seconds"),
    "fermentation": ("temperature", "time_seconds"),
    "rice_cooking": ("time_seconds",),
    "food_processor": ("time_seconds",),
    "puree": ("time_seconds",),
    "smoothie": ("time_seconds",),
    "turbo": ("time_seconds",),
}
_SETTING_KEYS = {"temperature": "temperature", "speed": "speed",
                 "time_seconds": "time", "weight_grams": "weight"}
_NUTRIENT_UNITS = {"calories": "kCal", "protein": "g", "carbohydrate": "g", "fat": "g"}


def build_step(step: dict[str, Any], index: int, locale: str) -> dict[str, Any]:
    """One step of ``{"text", "title"?, "program"?, <settings>}``."""
    out: dict[str, Any] = {
        # Without a title the recipe can never be published, not even privately.
        "title": step.get("title") or f"{word(locale, 'step')} {index}",
        "description": step.get("text") or "",
        "ingredients": [],
    }
    program = step.get("program")
    given = {k for k in (*_SETTING_KEYS, "reverse") if step.get(k) is not None}
    if not program:
        if given:
            raise ValueError(f"Step {index}: settings {sorted(given)} need a program")
        return out
    allowed = PROGRAMS[program]
    if extra := given - set(allowed):
        raise ValueError(f"Step {index}: program {program} does not use {sorted(extra)}")
    setting = {api: step[key] for key, api in _SETTING_KEYS.items() if step.get(key) is not None}
    if not setting:
        raise ValueError(f"Step {index}: program {program} needs one of {list(allowed)}")
    setting["order"] = 0
    if program == "customized":
        setting["clockwise"] = not step.get("reverse", False)
        setting = {"temperature": 0, "speed": 0, "time": 0, **setting}
    out["mode"] = {"type": program, "modeSetting": None, "deviceSettings": [setting]}
    return out


def build_groups(ingredients: list[dict[str, Any]], locale: str) -> list[dict[str, Any]]:
    """Ingredient groups; consecutive ingredients with the same ``group`` share one."""
    groups: list[dict[str, Any]] = []
    for item in ingredients:
        name = item.get("group") or word(locale, "ingredients")
        if not groups or groups[-1]["name"] != name:
            groups.append({"name": name, "isDefault": not groups,
                           "order": len(groups), "ingredients": []})
        entries = groups[-1]["ingredients"]
        unit, label = item.get("unit") or "", item["name"]
        if unit and unit not in options(locale)["ingredient_units"]:
            # The editor has no free-text unit (e.g. ml): keep it readable in the name.
            label, unit = f"{label} ({unit})", ""
        entries.append({"name": label, "amount": str(item.get("amount") or ""),
                        "unit": unit, "order": len(entries)})
    return groups


def apply_fields(payload: dict[str, Any], fields: dict[str, Any], locale: str) -> dict[str, Any]:
    """Overlay the simple recipe fields on a write payload (in place)."""
    valid = {o["id"] for o in options(locale)["categories"]}
    if bad := [c for c in fields.get("category_ids") or [] if c not in valid]:
        raise ValueError(f"Unknown category IDs for {locale}: {bad}")
    valid = {o["id"] for o in options(locale)["complexity"]}
    if fields.get("complexity_id") is not None and fields["complexity_id"] not in valid:
        raise ValueError(f"Unknown complexity ID for {locale}: {fields['complexity_id']}")

    for key, api in (("title", "title"), ("description", "description"),
                     ("complexity_id", "complexityId"), ("category_ids", "categoryIds")):
        if fields.get(key) is not None:
            payload[api] = fields[key]

    nutrients = {n["name"]: n for n in payload.get("nutrients") or []}
    for key, unit in _NUTRIENT_UNITS.items():
        if fields.get(key) is not None:
            nutrients[key] = {"name": key, "unit": unit, "amount": fields[key]}
    payload["nutrients"] = list(nutrients.values())

    serving_fields = {"servings": "amount", "preparation_minutes": "preparationTime",
                      "ready_minutes": "readyInTime"}
    if any(fields.get(k) is not None for k in (*serving_fields, "serving_unit", "ingredients", "steps")):
        sizes = payload.setdefault("servingSizes", [])
        if not sizes:
            # The API requires both times to be at least one minute.
            sizes.append({"isDefault": True, "amount": 4, "unit": serving_unit(locale, None),
                          "instruction": "", "preparationTime": 1, "readyInTime": 1,
                          "ingredientGroups": [], "steps": []})
        size = sizes[0]
        for key, api in serving_fields.items():
            if fields.get(key) is not None:
                size[api] = fields[key]
        if fields.get("serving_unit") is not None:
            size["unit"] = serving_unit(locale, fields["serving_unit"])
        if fields.get("ingredients") is not None:
            size["ingredientGroups"] = build_groups(fields["ingredients"], locale)
        if fields.get("steps") is not None:
            size["steps"] = [build_step(s, n, locale) for n, s in enumerate(fields["steps"], 1)]
    return payload


def new_payload(fields: dict[str, Any], locale: str) -> dict[str, Any]:
    """Write payload of a new recipe. Always a draft."""
    return apply_fields({
        "status": STATUS_DRAFT, "source": "member", "languageLocale": locale,
        "deviceTypeIds": [DEVICE_MC_SMART], "allowSocialSharing": False,
        "complexityId": default_complexity(locale), "categoryIds": [],
        "nutrients": [], "servingSizes": [],
    }, fields, locale)


def _image_ref(image: Any) -> dict[str, Any] | None:
    if not isinstance(image, dict):
        return None
    out: dict[str, Any] = {}
    for side in ("portrait", "landscape"):
        media = image.get(f"{side}Media") or {}
        out[f"{side}MediaId"] = media.get("id")
        out[f"{side}IsAiGenerated"] = bool(media.get("isAiGenerated"))
    return out if out["portraitMediaId"] or out["landscapeMediaId"] else None


def image_ref(media_id: int, ai_generated: bool) -> dict[str, Any]:
    return {"portraitMediaId": None, "landscapeMediaId": media_id,
            "portraitIsAiGenerated": ai_generated, "landscapeIsAiGenerated": ai_generated}


def write_payload(recipe: dict[str, Any]) -> dict[str, Any]:
    """Own recipe as read (GET) -> full body for PUT/POST. This is also the backup format."""
    payload: dict[str, Any] = {
        "status": recipe.get("status") or STATUS_DRAFT,
        "source": recipe.get("source") or "member",
        "languageLocale": recipe.get("languageLocale"),
        "deviceTypeIds": [d["id"] for d in recipe.get("deviceTypes") or []],
        "title": recipe.get("title") or "",
        "allowSocialSharing": bool(recipe.get("allowSocialSharing")),
        "categoryIds": [c["id"] for c in recipe.get("categories") or []],
        "nutrients": [{k: n.get(k) for k in ("name", "unit", "amount")}
                      for n in recipe.get("nutrients") or []],
        "servingSizes": [],
    }
    for key in ("description", "hintsAndTips"):
        if recipe.get(key) is not None:
            payload[key] = recipe[key]
    if complexity := (recipe.get("complexity") or {}).get("id"):
        payload["complexityId"] = complexity
    for key in ("thumbnail", "detailsImage"):
        if ref := _image_ref(recipe.get(key)):
            payload[key] = ref
    for size in recipe.get("servingSizes") or []:
        groups = []
        for group in size.get("ingredientGroups") or []:
            items = []
            for ing in group.get("ingredients") or []:
                item = {"name": ing.get("name") or "", "amount": str(ing.get("amount") or ""),
                        "unit": ing.get("unit") or "", "order": ing.get("order") or 0}
                if ing.get("systemIngredientId"):
                    item["systemIngredientId"] = ing["systemIngredientId"]
                if ing.get("isOptional"):
                    item["isOptional"] = True
                items.append(item)
            groups.append({"name": group.get("name") or "", "isDefault": bool(group.get("isDefault")),
                           "order": group.get("order") or 0, "ingredients": items})
        steps = []
        for step in size.get("steps") or []:
            out = {"title": step.get("title") or "", "description": step.get("description") or "",
                   "ingredients": []}
            if step.get("mode"):
                out["mode"] = {k: v for k, v in step["mode"].items() if k != "modules"}
            steps.append(out)
        payload["servingSizes"].append({
            "isDefault": size.get("isDefault", True), "amount": size.get("amount") or 0,
            "unit": size.get("unit") or "", "instruction": size.get("instruction") or "",
            "preparationTime": size.get("preparationTime") or 1,
            "readyInTime": size.get("readyInTime") or 1,
            "ingredientGroups": groups, "steps": steps,
        })
    return payload


# --- Compact views for people and language models ---------------------------

def recipe_summary(r: dict[str, Any]) -> dict[str, Any]:
    """A catalogue, favourite or own recipe in a list."""
    complexity = r.get("complexity")
    image = r.get("thumbnail") or r.get("detailsImage") or {}
    out = {
        "id": r.get("id"), "name": r.get("name") or r.get("title"), "status": r.get("status"),
        "url": r.get("url"), "source": r.get("source"),
        "preparation_minutes": r.get("preparationDuration") or r.get("preparationTime"),
        "ready_minutes": r.get("duration") or r.get("readyInTime"),
        "complexity": complexity.get("name") if isinstance(complexity, dict) else complexity,
        "rating": r.get("rating"), "ratings": r.get("totalRating"),
        "categories": [unescape(c.get("name") or "") for c in r.get("categories") or []],
        "devices": [d.get("code") or d.get("name") if isinstance(d, dict) else d
                    for d in r.get("deviceTypes") or []],
        "updated": r.get("lastUpdated") or r.get("updatedAt"),
        "image_url": image.get("landscape") or (image.get("landscapeMedia") or {}).get("url"),
    }
    return {k: v for k, v in out.items() if v not in (None, [])}


def _settings(step: dict[str, Any]) -> dict[str, Any] | None:
    device = step.get("deviceSetting") or step.get("mode")
    if not device:
        return None
    first = (device.get("settings") or device.get("deviceSettings") or [{}])[0]
    out: dict[str, Any] = {"program": device.get("mode") or device.get("type")}
    for key, api in _SETTING_KEYS.items():
        if first.get(api):
            out[key] = first[api]
    if device.get("reverse") or first.get("clockwise") is False:
        out["reverse"] = True
    return out


def recipe_details(r: dict[str, Any]) -> dict[str, Any]:
    """A catalogue or own recipe with ingredients and steps."""
    out = recipe_summary(r)
    out["description"] = r.get("description")
    out["hints"] = r.get("hintsAndTips")
    image = (r.get("detailsImage") or {})
    out["image_url"] = (image.get("landscape") or (image.get("landscapeMedia") or {}).get("url")
                        or out.get("image_url"))
    out["nutrients_per_serving"] = {n["name"]: f"{n['amount']} {n['unit']}" for n in r.get("nutrients") or []}
    sizes = []
    for size in r.get("servingSizes") or []:
        # Own recipes nest ingredients in their groups, catalogue recipes reference them.
        names = {g.get("id"): g.get("name") for g in size.get("ingredientGroups") or []}
        ingredients = [(i, names.get(i.get("ingredientGroupId"))) for i in size.get("ingredients") or []]
        for group in size.get("ingredientGroups") or []:
            ingredients += [(i, group.get("name")) for i in group.get("ingredients") or []]
        steps = []
        for step in size.get("steps") or []:
            item = {"title": step.get("name") or step.get("title"), "text": step.get("description")}
            if setting := _settings(step):
                item.update(setting)
            steps.append({k: v for k, v in item.items() if v not in (None, "")})
        sizes.append({
            "servings": size.get("amount"),
            "serving_unit": size.get("servingUnit") or size.get("unit"),
            "ingredients": [
                {k: v for k, v in {"group": group, "amount": i.get("amount"), "unit": i.get("unit"),
                                   "name": i.get("name")}.items() if v}
                for i, group in ingredients
            ],
            "steps": steps,
        })
    out["serving_sizes"] = sizes
    return {k: v for k, v in out.items() if v not in (None, [], {}, "")}
