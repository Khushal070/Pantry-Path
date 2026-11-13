"""Refine each candidate dish with the LLM: keep or drop it, merge duplicates, name its
ingredients the way a grocery store sells them, mark core vs common, add key ingredients
Food.com dropped (lasagna noodles, pie crust), and assign a cuisine.

Input: the dish pool (foodcom.pool_output). Output: one JSON record per dish in
catalog.refined_dir, which catalog_build.py turns into the catalog. A dish that fails
(API error, or an unusable reply even after one retry) is recorded with its error and
never raised, so one dish can't fail the whole run.

Shares come from the source data, never from the LLM: an ingredient's share is the
largest share among the raw names it covers (from_raw), capped at 1. Raw variants of one
item are mostly different recipes' wording ("flour" vs "all-purpose flour"); summing
would double-count recipes that list both, while the largest share is a safe lower
bound. Ingredients without source evidence are flagged added_by_llm with share 0.
"""

import json
import logging
import string
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from pantrypath_core.catalog import (
    CatalogIngredient,
    Cuisine,
    DishProfile,
    Role,
    normalize_name,
    slugify,
)
from pantrypath_core.schema import Category
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import DataCorruptError, DataUnavailableError, LLMError
from pantrypath_pipeline.llm import (
    LLMClient,
    LLMReply,
    LLMReplyError,
    Usage,
    get_client,
    reply_from_entry,
    usage_entry,
    write_json_atomic,
    write_usage_report,
)

log = logging.getLogger(__name__)

RETRY_NOTE = (
    "\n\nYour previous answer could not be used: {error}\n"
    "Answer again with one JSON object that follows the instructions above."
)


def _enum(values: Iterable) -> dict[str, Any]:
    return {"type": "string", "enum": [v.value for v in values]}


# JSON schema for the reply. Kept to the subset every provider's structured output takes:
# all objects closed (additionalProperties false), every field required, null via anyOf.
REFINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "keep": {"type": "boolean"},
        "merge_into": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "name": {"type": "string"},
        "aliases": {"type": "array", "items": {"type": "string"}},
        "cuisine": _enum(Cuisine),
        "ingredients": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "from_raw": {"type": "array", "items": {"type": "string"}},
                    "role": _enum(Role),
                    "category": _enum(Category),
                },
                "required": ["name", "from_raw", "role", "category"],
                "additionalProperties": False,
            },
        },
        "added": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": _enum(Role),
                    "category": _enum(Category),
                    "why": {"type": "string"},
                },
                "required": ["name", "role", "category", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["keep", "merge_into", "name", "aliases", "cuisine", "ingredients", "added"],
    "additionalProperties": False,
}


class _ReplyIngredient(BaseModel):
    name: str
    from_raw: list[str] = Field(default_factory=list)
    role: Role
    category: Category


class _ReplyAdded(BaseModel):
    name: str
    role: Role
    category: Category
    why: str = ""


class RefineReply(BaseModel):
    """What the LLM must return for one dish (validated before use)."""

    keep: bool
    merge_into: str | None = None
    name: str
    aliases: list[str] = Field(default_factory=list)
    cuisine: Cuisine
    ingredients: list[_ReplyIngredient] = Field(default_factory=list)
    added: list[_ReplyAdded] = Field(default_factory=list)


# ---------------------------------------------------------------- inputs


def load_pool(cfg: Config) -> dict[str, Any]:
    path = cfg.path(cfg["foodcom"]["pool_output"])
    if not path.is_file():
        raise DataUnavailableError(f"Dish pool not found at {path}; run select-dishes first")
    try:
        pool = json.loads(path.read_text())
    except ValueError as exc:
        raise DataCorruptError(
            f"Dish pool {path} is not valid JSON ({exc}); rerun select-dishes"
        ) from exc
    if not isinstance(pool, dict) or not isinstance(pool.get("dishes"), list):
        raise DataCorruptError(f"Dish pool {path} has no 'dishes' list; rerun select-dishes")
    return pool


def load_template(cfg: Config) -> tuple[string.Template, str]:
    """The fixed prompt and its version (first line: 'prompt_version: ...')."""
    path = cfg.path(cfg["llm"]["prompt_file"])
    if not path.is_file():
        raise DataUnavailableError(f"Prompt file not found: {path}")
    text = path.read_text()
    first = text.splitlines()[0] if text else ""
    version = first.split(":", 1)[1].strip() if first.startswith("prompt_version:") else "none"
    return string.Template(text), version


def refined_path(cfg: Config, dish_id: str) -> Path:
    return cfg.path(cfg["catalog"]["refined_dir"]) / f"{dish_id}.json"


def _popularity(dish: dict[str, Any]) -> tuple[int, int, str]:
    """Total order used everywhere: reviews, then recipes, then dish_id."""
    return (int(dish.get("review_count") or 0), int(dish.get("recipe_count") or 0),
            dish["dish_id"])


def merge_candidates(dish: dict[str, Any], pool_dishes: list[dict[str, Any]], *,
                     limit: int, min_score: float) -> list[dict[str, Any]]:
    """More popular pool dishes whose names look alike: the only allowed merge targets.

    Only more popular dishes, so merges always point one way (no cycles), and only
    look-alikes, to keep the prompt short.
    """
    own = _popularity(dish)
    scored = []
    for other in pool_dishes:
        if _popularity(other) <= own:
            continue
        score = fuzz.token_set_ratio(dish["name"], other["name"])
        if score >= min_score:
            scored.append((score, _popularity(other), other))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [other for _, _, other in scored[:limit]]


def describe_dish(dish: dict[str, Any]) -> str:
    lines = [
        f"name: {dish['name']}",
        f"cuisine hint from Food.com keywords: {dish.get('cuisine_hint') or 'none'}",
        f"recipes: {dish.get('recipe_count', 0)}, reviews: {dish.get('review_count', 0)}",
    ]
    titles = dish.get("sample_titles") or []
    if titles:
        lines.append("sample recipe titles: " + "; ".join(titles[:8]))
    ingredients = dish.get("ingredients") or []
    if ingredients:
        lines.append("raw ingredients (share of recipes):")
        lines += [f"- {i['name']}: {float(i['share']):.2f}" for i in ingredients]
    else:
        lines.append("raw ingredients: (none)")
    return "\n".join(lines)


def render_prompt(cfg: Config, template: string.Template, dish: dict[str, Any],
                  candidates: list[dict[str, Any]]) -> str:
    settings = cfg["catalog"]
    merge_lines = "\n".join(f"- {c['dish_id']}: {c['name']}" for c in candidates)
    return template.substitute(
        cuisines=", ".join(c.value for c in Cuisine),
        categories=", ".join(c.value for c in Category),
        core_share=settings["core_share"],
        common_share=settings["common_share"],
        dish=describe_dish(dish),
        merge_candidates=merge_lines or "(none)",
    )


# ---------------------------------------------------------------- reply -> profile


def _key(name: str) -> str:
    return " ".join(str(name).lower().split())


def build_profile(reply: RefineReply, dish: dict[str, Any], *, source: str,
                  catalog_version: str) -> tuple[DishProfile, list[str]]:
    """Turn a validated reply into a DishProfile; shares are computed from the pool data."""
    raw_shares = {_key(r["name"]): min(float(r.get("share") or 0), 1.0)
                  for r in dish.get("ingredients") or []}
    notes: list[str] = []
    merged: dict[str, dict[str, Any]] = {}

    def add(name: str, role: Role, category: Category, share: float, by_llm: bool) -> None:
        name = normalize_name(name)
        if name in merged:  # the LLM listed one item twice: keep one entry
            prev = merged[name]
            prev["role"] = Role.core if Role.core in (prev["role"], role) else Role.common
            prev["share"] = max(prev["share"], share)
            prev["added_by_llm"] = prev["added_by_llm"] and by_llm
            notes.append(f"merged duplicate ingredient {name!r}")
            return
        merged[name] = {"name": name, "role": role, "category": category, "share": share,
                        "added_by_llm": by_llm}

    for ing in reply.ingredients:
        known = [raw_shares[_key(r)] for r in ing.from_raw if _key(r) in raw_shares]
        unknown = [r for r in ing.from_raw if _key(r) not in raw_shares]
        if unknown:
            notes.append(f"{ing.name}: from_raw not in the source list: {unknown}")
        add(ing.name, ing.role, ing.category, max(known, default=0.0), by_llm=not known)
    for extra in reply.added:
        if _key(extra.name) in merged:
            notes.append(f"added {extra.name!r} is already listed; ignored")
            continue
        add(extra.name, extra.role, extra.category, 0.0, by_llm=True)

    ordered = sorted(merged.values(),
                     key=lambda i: (i["role"] != Role.core, -i["share"], i["name"]))
    profile = DishProfile(
        dish_id=dish["dish_id"],
        name=reply.name,
        # The Food.com group name stays findable as an alias
        aliases=[a for a in [*reply.aliases, dish.get("name") or ""] if a.strip()],
        cuisine=reply.cuisine,
        ingredients=[CatalogIngredient(**i) for i in ordered],
        recipe_count=int(dish.get("recipe_count") or 0),
        review_count=int(dish.get("review_count") or 0),
        source=source,
        catalog_version=catalog_version,
    )
    return profile, notes


def _apply_reply(record: dict[str, Any], data: dict[str, Any], dish: dict[str, Any],
                 allowed_merges: set[str], *, source: str, catalog_version: str) -> None:
    """Validate a reply and fill the record. Raises ValueError (incl. pydantic's
    ValidationError) when the reply can't be used, which triggers the retry."""
    reply = RefineReply.model_validate(data)
    notes: list[str] = []
    merge_into = reply.merge_into
    if merge_into and merge_into not in allowed_merges:
        notes.append(f"ignored merge_into {merge_into!r}: not one of the candidates shown")
        merge_into = None
    record.update(
        keep=reply.keep,
        merge_into=merge_into if reply.keep else None,
        name=normalize_name(reply.name),
        aliases=[normalize_name(a) for a in reply.aliases if a.strip()],
        profile=None,
        notes=notes,
    )
    if not reply.keep:
        return
    try:
        profile, profile_notes = build_profile(reply, dish, source=source,
                                               catalog_version=catalog_version)
    except ValueError:
        if merge_into:  # a merged dish only lends its name and counts; no profile needed
            notes.append("profile invalid; record kept for the merge only")
            return
        raise
    record["profile"] = profile.model_dump(mode="json")
    notes += profile_notes


def _short_error(exc: Exception, limit: int = 600) -> str:
    return " ".join(str(exc).split())[:limit]


def refine_dish(cfg: Config, dish: dict[str, Any], candidates: list[dict[str, Any]],
                client: LLMClient, *, source: str = "foodcom",
                template: tuple[string.Template, str] | None = None,
                catalog_version: str | None = None) -> dict[str, Any]:
    """Ask the LLM about one dish (one retry on an unusable reply); never raises LLMError."""
    tmpl, version = template or load_template(cfg)
    catalog_version = catalog_version or datetime.now(UTC).date().isoformat()
    prompt = render_prompt(cfg, tmpl, dish, candidates)
    allowed = {c["dish_id"] for c in candidates} - {dish["dish_id"]}
    record: dict[str, Any] = {
        "dish_id": dish["dish_id"], "prompt_version": version,
        "refined_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "keep": False, "merge_into": None, "name": dish["name"], "aliases": [],
        "profile": None, "notes": [], "error": None,
    }
    replies: list[LLMReply] = []
    error: str | None = None
    for attempt in range(2):
        try:
            reply = client.complete_json(prompt, REFINE_SCHEMA,
                                         purpose=f"refine_dish:{dish['dish_id']}")
        except LLMReplyError as exc:  # answered, but unusable: worth one retry
            replies.append(exc.reply)
            error = str(exc)
        except LLMError as exc:  # API failed even after its own retries: give up on this dish
            error = str(exc)
            break
        else:
            replies.append(reply)
            try:
                _apply_reply(record, reply.data, dish, allowed, source=source,
                             catalog_version=catalog_version)
                error = None
                break
            except ValueError as exc:
                error = f"reply failed validation: {_short_error(exc)}"
        if attempt == 0:
            prompt += RETRY_NOTE.format(error=error)

    record["calls"] = [usage_entry(r) for r in replies]
    record["usage"] = asdict(sum((r.usage for r in replies), Usage()))
    record["model"] = replies[-1].model if replies else None
    record["provider"] = replies[-1].provider if replies else None
    record["cached"] = bool(replies) and all(r.cached for r in replies)
    if error:
        record.update(keep=False, merge_into=None, profile=None, error=error)
        log.warning("Dish %s not refined: %s", dish["dish_id"], error)
    return record


def replies_from_records(records: Iterable[dict[str, Any]]) -> list[LLMReply]:
    """Every LLM call recorded in refined records (for the usage report)."""
    return [reply_from_entry(call) for rec in records for call in rec.get("calls") or []]


# ---------------------------------------------------------------- steps


def _refine_and_write(cfg: Config, dish: dict[str, Any], pool_dishes: list[dict[str, Any]],
                      client: LLMClient, template: tuple[string.Template, str]) -> dict[str, Any]:
    settings = cfg["catalog"]
    candidates = merge_candidates(
        dish, pool_dishes, limit=int(settings.get("merge_candidates", 15)),
        min_score=float(settings.get("merge_min_score", 50)),
    )
    record = refine_dish(cfg, dish, candidates, client, template=template)
    write_json_atomic(refined_path(cfg, dish["dish_id"]), record)
    return record


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in ("dish_id", "keep", "merge_into", "cached", "error")}


def run_one(cfg: Config, dish_id: str, client: LLMClient | None = None) -> dict[str, Any]:
    """Refine one pool dish (one Airflow mapped task)."""
    pool = load_pool(cfg)
    dishes = {d["dish_id"]: d for d in pool["dishes"]}
    if dish_id not in dishes:
        raise DataUnavailableError(f"Dish {dish_id!r} is not in the dish pool; rerun select-dishes")
    record = _refine_and_write(cfg, dishes[dish_id], pool["dishes"], client or get_client(cfg),
                               template=load_template(cfg))
    return _summary(record)


def run_all(cfg: Config, client: LLMClient | None = None) -> dict[str, Any]:
    """Refine every pool dish in parallel (llm.concurrency), then write the usage report."""
    pool = load_pool(cfg)
    client = client or get_client(cfg)
    template = load_template(cfg)
    workers = max(int(cfg["llm"].get("concurrency", 4)), 1)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        records = list(executor.map(
            lambda d: _refine_and_write(cfg, d, pool["dishes"], client, template),
            pool["dishes"],
        ))
    report = write_usage_report(cfg, replies_from_records(records))
    return {
        "dishes": len(records),
        "kept": sum(1 for r in records if r["keep"] and not r["merge_into"]),
        "merged": sum(1 for r in records if r["keep"] and r["merge_into"]),
        "dropped": sum(1 for r in records if not r["keep"] and not r["error"]),
        "failed": sum(1 for r in records if r["error"]),
        "llm_calls_real": report["totals"]["calls_real"],
        "llm_calls_cached": report["totals"]["calls_cached"],
        "llm_est_cost_usd": report["totals"]["est_cost_usd"],
    }


def profile_for_unknown_dish(cfg: Config, dish_name: str,
                             client: LLMClient | None = None,
                             template: tuple[string.Template, str] | None = None) -> DishProfile:
    """Request-time fallback for a dish that isn't in the catalog.

    Same prompt with no Food.com data, so every ingredient comes from the LLM: the
    profile has source 'llm' and every ingredient is added_by_llm. Replies are cached in
    llm.request_cache_dir, apart from the versioned batch cache. Raises LLMError when no
    usable profile comes back. `template` (text, version) replaces the refine prompt, e.g.
    one written for a dish name with no recipe data; the schema, validation and retry stay
    the same. Without it the prompt is prompts/refine_dish.txt, as before.
    """
    try:
        name = normalize_name(dish_name)
    except ValueError as exc:
        raise LLMError("The dish name is empty") from exc
    dish_id = slugify(name)
    if not dish_id:
        raise LLMError(f"{dish_name!r} doesn't look like a dish name")
    if client is None:
        settings = cfg["llm"]
        cache_dir = settings.get("request_cache_dir") or settings["cache_dir"]
        client = get_client(cfg, cache_dir=cfg.path(cache_dir))
    dish = {"dish_id": dish_id, "name": name, "cuisine_hint": None, "recipe_count": 0,
            "review_count": 0, "sample_titles": [], "ingredients": []}
    record = refine_dish(cfg, dish, [], client, source="llm", catalog_version="request",
                         template=template)
    if record["error"]:
        raise LLMError(f"Could not get ingredients for {dish_name!r} from the LLM: "
                       f"{record['error']}")
    if not record["keep"] or record["profile"] is None:
        raise LLMError(f"The LLM says {dish_name!r} is not a dish it can list ingredients for")
    return DishProfile.model_validate(record["profile"])
