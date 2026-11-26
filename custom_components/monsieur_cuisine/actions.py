"""What the integration can do with recipes.

One table (``ACTIONS``) feeds both the Home Assistant actions (``services.py``)
and the tools for language models (``llm_api.py``), so voice, MCP and
automations share names, schemas and behaviour. Deliberately absent: deleting
recipes, submitting them for public review, raw API access and controlling the
machine.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from ipaddress import ip_address
import re
import socket
from typing import Any

import aiohttp
import voluptuous as vol
from yarl import URL

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_LOCALE, DATA_ACCOUNT, DATA_LIVE, DOMAIN, EVENT_RECIPE_SENT
from .mcbridge import build_payload, from_html, from_schema_org
from .mcbridge.account import planner_events, shopping_entries
from .mcbridge.authoring import (
    OWN_STATUSES,
    PROGRAMS,
    STATUS_DRAFT,
    STATUS_PRIVATE,
    STEP_RULES,
    apply_fields,
    image_ref,
    new_payload,
    recipe_details,
    recipe_summary,
    write_payload,
)
from .mcbridge.client import McApiError, McAuthError, McClient
from .mcbridge.locales import DEFAULT_LOCALE, options

MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (Home Assistant)"
SITE_HOSTS = {"www.monsieur-cuisine.com", "monsieur-cuisine.com"}


@dataclass
class Ctx:
    """Everything an action needs about the account it runs for."""

    hass: HomeAssistant
    entry: ConfigEntry
    # Called by a language model: its URLs come from untrusted text, so they
    # must not reach the home network, nor may it read local files.
    untrusted: bool = False

    @property
    def data(self) -> dict[str, Any]:
        return self.hass.data[DOMAIN][self.entry.entry_id]

    @property
    def client(self) -> McClient:
        return self.data["client"]

    @property
    def locale(self) -> str:
        return self.entry.data.get(CONF_LOCALE, DEFAULT_LOCALE)


@dataclass(frozen=True)
class Action:
    """One capability."""

    name: str
    title: str
    description: str
    schema: dict[Any, Any]
    func: Callable[[Ctx, dict[str, Any]], Awaitable[dict[str, Any]]]
    read_only: bool = False
    destructive: bool = False
    # Fields for administrators only, hidden from language models.
    admin_schema: dict[Any, Any] | None = None


async def async_run(hass: HomeAssistant, action: Action, args: dict[str, Any], *,
                    untrusted: bool = False) -> dict[str, Any]:
    """Run an action for the configured account and translate its errors."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("Monsieur Cuisine is not set up or not connected")
    entry = entries[0]
    try:
        return await action.func(Ctx(hass, entry, untrusted), args)
    except McAuthError as err:
        entry.async_start_reauth(hass)
        raise HomeAssistantError("Monsieur Cuisine: the Lidl sign-in expired") from err
    except (McApiError, aiohttp.ClientError, TimeoutError) as err:
        raise HomeAssistantError(f"Monsieur Cuisine: {err}") from err
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err


# --- Fetching ----------------------------------------------------------------

async def _check_public(hass: HomeAssistant, url: URL) -> None:
    if url.scheme not in ("http", "https") or not url.host:
        raise ValueError("Only http and https addresses are supported")
    infos = await hass.loop.getaddrinfo(url.host, url.port, type=socket.SOCK_STREAM)
    if not all(ip_address(info[4][0].split("%")[0]).is_global for info in infos):
        raise ValueError("The address is not on the public internet")


async def _fetch(ctx: Ctx, address: str, limit: int) -> bytes:
    """GET a page or picture; for untrusted callers every hop must be public."""
    session, url = async_get_clientsession(ctx.hass), URL(address)
    async with asyncio.timeout(30):
        for _ in range(MAX_REDIRECTS):
            if ctx.untrusted:
                await _check_public(ctx.hass, url)
            async with session.get(url, headers={"user-agent": USER_AGENT},
                                   allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    url = url.join(URL(resp.headers["location"]))
                    continue
                if resp.status != 200:
                    raise ValueError(f"{url.host} answered {resp.status}")
                content = await resp.content.read(limit + 1)
                while len(content) <= limit and (more := await resp.content.read(limit + 1 - len(content))):
                    content += more
                if len(content) > limit:
                    raise ValueError("The download is too large")
                return content
    raise ValueError("Too many redirects")


def _image_mime(content: bytes) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("The picture must be a JPEG, PNG or WebP file")


async def _recipe_id(ctx: Ctx, args: dict[str, Any]) -> int:
    """Recipe ID from ``recipe_id`` or a monsieur-cuisine.com page address."""
    if "recipe_id" in args:
        return args["recipe_id"]
    if not (address := args.get("url")):
        raise ValueError("recipe_id or url is required")
    url = URL(address)
    if url.scheme != "https" or url.host not in SITE_HOSTS:
        raise ValueError("url must be an https page on monsieur-cuisine.com")
    page = (await _fetch(ctx, address, MAX_PAGE_BYTES)).decode(errors="replace")
    match = re.search(r'recipeId["\']?\s*:\s*["\']?(\d+)', page)
    if not match:
        raise ValueError("The page does not contain a recipe")
    return int(match.group(1))


# --- Catalogue ---------------------------------------------------------------

async def _search(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    data = await ctx.client.search_recipes(
        args["query"], args["page"], device="MC3.0" if args["smart_only"] else None)
    return {"total": data.get("total"), "page": data.get("currentPage"),
            "pages": data.get("totalPage"), "language": data.get("language"),
            "recipes": [recipe_summary(r) for r in data.get("recipes") or []]}


async def _get_recipe(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    return recipe_details(await ctx.client.get_recipe(await _recipe_id(ctx, args)))


# --- Own recipes -------------------------------------------------------------

async def _list_own(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    data = await ctx.client.list_own_recipes(
        args["page"], 20, args["query"], [args["status"]] if "status" in args else OWN_STATUSES)
    return {"total": data.get("total"), "page": data.get("currentPage"), "pages": data.get("totalPage"),
            "recipes": [recipe_summary(r) for r in data.get("recipes") or []]}


async def _own(ctx: Ctx, recipe_id: int) -> dict[str, Any]:
    recipe = await ctx.client.get_own_recipe(recipe_id)
    if not recipe:
        raise ValueError(f"Recipe {recipe_id} is not one of your own recipes")
    return recipe


async def _get_own(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    return recipe_details(await _own(ctx, args["recipe_id"]))


async def _export(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    recipe = await _own(ctx, args["recipe_id"])
    return {"recipe_id": recipe["id"], "payload": write_payload(recipe)}


async def _saved(ctx: Ctx, recipe_id: int) -> dict[str, Any]:
    recipe = await _own(ctx, recipe_id)
    await ctx.data["coordinator"].async_request_refresh()
    return {"recipe_id": recipe_id, "status": recipe.get("status"), "recipe": recipe_details(recipe)}


async def _create(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    if "payload" in args:
        # A restored backup is a new recipe, never a copy of its old status.
        payload = {**args["payload"], "status": STATUS_DRAFT}
        if not payload.get("title"):
            raise ValueError("The payload has no title")
    else:
        if not args.get("title"):
            raise ValueError("title is required")
        payload = new_payload(args, ctx.locale)
    if args["dry_run"]:
        return {"dry_run": True, "payload": payload}
    created = await ctx.client.create_recipe(payload)
    return await _saved(ctx, created["id"])


async def _update(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    current = write_payload(await _own(ctx, args["recipe_id"]))
    if "payload" in args:
        payload = {**args["payload"], "status": current["status"]}
    else:
        payload = apply_fields(current, args, current.get("languageLocale") or ctx.locale)
    if args["dry_run"]:
        return {"dry_run": True, "recipe_id": args["recipe_id"], "payload": payload}
    await ctx.client.update_recipe(args["recipe_id"], payload)
    return await _saved(ctx, args["recipe_id"])


async def _set_image(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    payload = write_payload(await _own(ctx, args["recipe_id"]))
    if path := args.get("image_path"):
        if ctx.untrusted or not ctx.hass.config.is_allowed_path(path):
            raise ValueError("Home Assistant may not read this path")

        def read() -> bytes:
            with open(path, "rb") as file:
                return file.read(MAX_IMAGE_BYTES + 1)
        content = await ctx.hass.async_add_executor_job(read)
        if len(content) > MAX_IMAGE_BYTES:
            raise ValueError("The picture is too large")
    elif url := args.get("image_url"):
        content = await _fetch(ctx, url, MAX_IMAGE_BYTES)
    else:
        raise ValueError("image_url is required")
    media_id = await ctx.client.upload_image(content, _image_mime(content))
    payload["thumbnail"] = payload["detailsImage"] = image_ref(media_id, args["ai_generated"])
    await ctx.client.update_recipe(args["recipe_id"], payload)
    return await _saved(ctx, args["recipe_id"])


async def _publish_privately(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    recipe = await _own(ctx, args["recipe_id"])
    if recipe.get("status") == STATUS_PRIVATE:
        return await _saved(ctx, args["recipe_id"])
    if recipe.get("status") != STATUS_DRAFT:
        raise ValueError(f"Only drafts can be published privately, this one is {recipe.get('status')}")
    if "detailsImage" not in write_payload(recipe):
        # The API would accept it, but then reject every later change.
        raise ValueError("The recipe has no picture yet, use set_recipe_image first")
    await ctx.client.set_recipe_status(args["recipe_id"], STATUS_PRIVATE)
    return await _saved(ctx, args["recipe_id"])


async def _options(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    return {"locale": ctx.locale, **options(ctx.locale),
            "programs": {name: list(params) for name, params in PROGRAMS.items()}}


async def _send(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    if url := args.get("url"):
        recipe = from_html((await _fetch(ctx, url, MAX_PAGE_BYTES)).decode(errors="replace"), ctx.locale)
    elif data := args.get("recipe"):
        recipe = from_schema_org(data, ctx.locale)
    else:
        raise ValueError("url or recipe is required")
    response: dict[str, Any] = {
        "title": recipe.title,
        "steps": [{"title": s.title, "setting": s.setting.describe() if s.setting else None}
                  for s in recipe.steps],
        "warnings": recipe.warnings,
        "image_url": recipe.image_url,
    }
    if args["dry_run"]:
        response["preview"] = recipe.preview()
        return response
    if rid := args.get("recipe_id"):
        existing = await _own(ctx, rid)
        result = await ctx.client.update_recipe(rid, build_payload(
            recipe, status=existing.get("status") or STATUS_DRAFT, existing=existing, locale=ctx.locale))
    else:
        # New recipes are drafts: private publishing requires a picture.
        result = await ctx.client.create_recipe(build_payload(recipe, locale=ctx.locale))
    response.update(recipe_id=result.get("id"), status=result.get("status"))
    ctx.hass.bus.async_fire(EVENT_RECIPE_SENT, {"recipe_id": result.get("id"), "title": recipe.title})
    await ctx.data["coordinator"].async_request_refresh()
    return response


# --- Favourites and account --------------------------------------------------

async def _list_favorites(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    data = await ctx.client.list_favorites(args["page"])
    return {"total": data.get("total"), "page": data.get("currentPage"), "pages": data.get("totalPage"),
            "recipes": [recipe_summary(b["recipe"]) for b in data.get("bookmarks") or [] if b.get("recipe")]}


async def _favorite(ctx: Ctx, args: dict[str, Any], *, add: bool) -> dict[str, Any]:
    # Favourites are keyed by the translation, not by the recipe.
    recipe = await ctx.client.get_recipe(await _recipe_id(ctx, args))
    await (ctx.client.add_favorite if add else ctx.client.remove_favorite)(recipe["translationId"])
    return {"recipe_id": recipe["id"], "name": recipe.get("name"), "favorite": add}


async def _add_favorite(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    return await _favorite(ctx, args, add=True)


async def _remove_favorite(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    return await _favorite(ctx, args, add=False)


async def _profile(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    user = await ctx.client.get_profile()
    return {"id": user.get("id"), "display_name": user.get("displayName"),
            "first_name": user.get("firstName"), "last_name": user.get("lastName"),
            "email": user.get("email"),
            "rejected_recipes": (user.get("alerts") or {}).get("rejectedRecipeCount")}


async def _shopping(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    entries = shopping_entries(await ctx.client.get_shopping_list())
    return {"items": [{"item": e.summary, "done": e.ticked} for e in entries]}


async def _planner(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    account = ctx.data[DATA_ACCOUNT]
    await account.async_refresh()
    if not account.last_update_success:
        raise HomeAssistantError("Monsieur Cuisine: the weekly planner could not be read")
    return {"meals": [{"date": e.day.isoformat(), "recipe": e.title}
                      for e in planner_events(account.data.get("planner"))]}


async def _status(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    live = ctx.data[DATA_LIVE]
    out: dict[str, Any] = {"devices": [
        {"model": d.get("modelName"), "cloud_connected": d.get("iotConnectionStatus") == "CONNECT"}
        for d in ctx.data["coordinator"].data["devices"].values()]}
    status = None if live.stale else live.data
    if status is None:
        return {**out, "cooking": None, "note": "No live data: the machine is probably switched off"}
    out["cooking"] = {k: v for k, v in {
        "state": status.command_type, "mode": status.cook_mode, "preheating": status.is_preheat,
        "target_temperature": status.target_temp, "speed": status.speed,
        "remaining_seconds": status.remaining_seconds, "recipe_id": status.recipe_id,
        "step": status.step_index, "steps": status.total_step_index}.items() if v is not None}
    return out


# --- Schemas -----------------------------------------------------------------

def _opt(key: str, description: str, **kw: Any) -> vol.Optional:
    return vol.Optional(key, description=description, **kw)


def _req(key: str, description: str) -> vol.Required:
    return vol.Required(key, description=description)


_POSITIVE = vol.All(vol.Coerce(int), vol.Range(min=1))
_PAGE = {_opt("page", "Result page, starting at 1", default=1): _POSITIVE}
_OWN_ID = {_req("recipe_id", "ID of one of your own recipes"): _POSITIVE}
_ANY_ID = {_opt("recipe_id", "Recipe ID, e.g. from search_recipes"): _POSITIVE,
           _opt("url", "Address of the recipe page on monsieur-cuisine.com"): str}
_DRY_RUN = {_opt("dry_run", "Only return what would be sent, change nothing", default=False): vol.Coerce(bool)}

_INGREDIENT = vol.Schema({
    _req("name", "Ingredient"): str,
    _opt("amount", "Number as text, e.g. 250 or 1.5"): vol.Coerce(str),
    _opt("unit", "One of ingredient_units from get_recipe_options"): str,
    _opt("group", "Heading of the ingredient group, e.g. the sauce"): str,
})
_STEP = vol.Schema({
    _req("text", "What to do, with the amount of what is added (at most 240 characters)"): vol.All(str, vol.Length(max=240)),
    _opt("title", "Short heading (at most 80 characters); without one the screen shows only the number"): vol.All(str, vol.Length(max=80)),
    _opt("program", "Machine program; leave out for a manual step"): vol.In(list(PROGRAMS)),
    _opt("temperature", "°C, up to 130"): vol.All(vol.Coerce(int), vol.Range(min=0, max=130)),
    _opt("speed", "Speed 0-10"): vol.All(vol.Coerce(int), vol.Range(min=0, max=10)),
    _opt("time_seconds", "Duration in seconds"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    _opt("weight_grams", "Weight for the scale program"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    _opt("reverse", "Counter-clockwise, only for customized"): vol.Coerce(bool),
})
_FIELDS = {
    _opt("title", "Recipe title (at most 80 characters)"): vol.All(str, vol.Length(min=1, max=80)),
    _opt("description", "Short description (at most 240 characters)"): vol.All(str, vol.Length(max=240)),
    _opt("servings", "Number of servings"): _POSITIVE,
    _opt("serving_unit", "One of serving_units from get_recipe_options"): str,
    _opt("preparation_minutes", "Working time in minutes"): _POSITIVE,
    _opt("ready_minutes", "Total time in minutes"): _POSITIVE,
    _opt("complexity_id", "ID from get_recipe_options"): _POSITIVE,
    _opt("category_ids", "IDs from get_recipe_options"): [_POSITIVE],
    _opt("calories", "kcal per serving"): vol.All(vol.Coerce(int), vol.Range(min=0)),
    _opt("protein", "Grams per serving"): vol.All(vol.Coerce(int), vol.Range(min=0)),
    _opt("carbohydrate", "Grams per serving"): vol.All(vol.Coerce(int), vol.Range(min=0)),
    _opt("fat", "Grams per serving"): vol.All(vol.Coerce(int), vol.Range(min=0)),
    _opt("ingredients", "All ingredients in order (replaces the existing ones)"): [_INGREDIENT],
    _opt("steps", "All steps in order (replaces the existing ones)"): [_STEP],
}
_PAYLOAD = {_opt("payload", "Backup from export_recipe, instead of the single fields"): dict}

ACTIONS = [
    Action("search_recipes", "Search recipes",
           "Search the public Monsieur Cuisine recipe catalogue in the account's language.",
           {_req("query", "Search words, e.g. zupa pomidorowa"): vol.All(str, vol.Length(min=1)),
            _opt("smart_only", "Only recipes made for the Monsieur Cuisine Smart", default=False): vol.Coerce(bool),
            **_PAGE}, _search, read_only=True),
    Action("get_recipe", "Get a recipe",
           "Ingredients and steps of a public recipe. Give recipe_id or url.",
           _ANY_ID, _get_recipe, read_only=True),
    Action("list_own_recipes", "List own recipes", "Recipes written in this account.",
           {_opt("query", "Filter by words", default=""): str,
            _opt("status", "Filter by status"): vol.In(list(OWN_STATUSES)), **_PAGE},
           _list_own, read_only=True),
    Action("get_own_recipe", "Get an own recipe", "Ingredients and steps of one of your own recipes.",
           _OWN_ID, _get_own, read_only=True),
    Action("export_recipe", "Export an own recipe",
           "Complete backup of one of your own recipes, accepted by create_recipe and update_recipe.",
           _OWN_ID, _export, read_only=True),
    Action("get_recipe_options", "Recipe options",
           "Valid categories, complexity levels, units and machine programs for writing recipes.",
           {}, _options, read_only=True),
    Action("create_recipe", "Create a recipe",
           "Create a new own recipe as a private draft. It reaches the machine only after "
           "set_recipe_image and publish_recipe_privately. " + STEP_RULES,
           {**_FIELDS, **_DRY_RUN}, _create, admin_schema=_PAYLOAD),
    Action("update_recipe", "Update an own recipe",
           "Change fields of one of your own recipes; fields not given stay as they are, but ingredients "
           "and steps replace the whole list, so send all of them. Read the recipe with get_own_recipe "
           "first, the user may have changed it in the app. " + STEP_RULES,
           {**_OWN_ID, **_FIELDS, **_DRY_RUN}, _update, destructive=True, admin_schema=_PAYLOAD),
    Action("set_recipe_image", "Set the recipe picture",
           "Upload a JPEG, PNG or WebP picture and use it as the picture of one of your own recipes.",
           {**_OWN_ID, _opt("image_url", "Public address of the picture"): str,
            _opt("ai_generated", "Mark the picture as AI-generated", default=False): vol.Coerce(bool)},
           _set_image, destructive=True,
           admin_schema={_opt("image_path", "File Home Assistant may read, instead of image_url"): str}),
    Action("publish_recipe_privately", "Publish a recipe privately",
           "Make one of your own drafts visible on your machine, only for this account. Needs a picture, "
           "categories, ingredients and steps. It can not become a draft again, so confirm with the user first.",
           _OWN_ID, _publish_privately, destructive=True),
    Action("send_recipe", "Import a recipe",
           "Convert a recipe web page (schema.org) into an own draft with machine steps. "
           "Use dry_run first and check the steps.",
           {_opt("url", "Public recipe page"): str,
            _opt("recipe_id", "Own recipe to overwrite instead of creating a new one"): _POSITIVE, **_DRY_RUN},
           _send, destructive=True,
           admin_schema={_opt("recipe", "schema.org/Recipe object instead of url"): dict}),
    Action("list_favorites", "List favourites", "Favourite recipes of the account.",
           _PAGE, _list_favorites, read_only=True),
    Action("add_favorite", "Add a favourite", "Add a public recipe to the favourites. Give recipe_id or url.",
           _ANY_ID, _add_favorite),
    Action("remove_favorite", "Remove a favourite",
           "Remove a recipe from the favourites. Give recipe_id or url.", _ANY_ID, _remove_favorite),
    Action("get_profile", "Get the profile", "Name and e-mail of the Monsieur Cuisine account.",
           {}, _profile, read_only=True),
    Action("get_shopping_list", "Get the shopping list", "Shopping list of the account.",
           {}, _shopping, read_only=True),
    Action("get_meal_plan", "Get the meal plan", "Recipes planned for the next 30 days.",
           {}, _planner, read_only=True),
    Action("get_status", "Get the cooking status", "What the machine is doing right now.",
           {}, _status, read_only=True),
]
