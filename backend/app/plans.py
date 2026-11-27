"""Dish plans: the steps of a guided build and the options in each.

A plan comes from one of three places, fastest first:

    catalog    a catalog dish's ingredients, grouped into steps by food category; core
               ingredients (and common ones in at least half the recipes) start ticked;
               config/variants.yaml turns a generic one into kinds (sandwich bread ->
               white, whole wheat, sourdough, ...)
    template   config/dish_templates.yaml, for a kind of food rather than one recipe
               ("sandwich", "salad", "pasta"): build your own
    model      one model call for any other dish (the engine's plan()), in the same shape

Every option is screened against the shopper's allergens and diet (screen.py). A clashing
option is left out, with the reason, and its swap is offered in its place when there is
one ("cheddar cheese" -> "dairy-free cheese" for a vegan), ticked if the original was.
"""

import re
from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from app.screen import Screen
from pantrypath_core.schema import Category

CONFIG = Path(__file__).resolve().parents[1] / "config"
MAX_STEPS, MAX_OPTIONS = 6, 8
CATEGORIES = {c.value for c in Category}
# Steps for catalog dishes, in order: (id, label, catalog categories)
GROUPS = [
    ("bread", "Bread and grains", {"bakery", "grain"}),
    ("protein", "Protein", {"meat", "seafood", "egg", "legume"}),
    ("vegetables", "Vegetables and fruit", {"produce"}),
    ("dairy", "Cheese and dairy", {"dairy"}),
    ("sauces", "Sauces", {"condiment"}),
    ("pantry", "Pantry basics", {"spice_herb", "baking", "oil_fat", "sweetener", "nut_seed",
                                 "beverage", "other"}),
]
USUAL_SHARE = 0.5


class Option(BaseModel):
    name: str
    category: str = "other"
    usual: bool = False
    share: float | None = None          # "in 72% of recipes": catalog dishes only
    instead_of: str | None = None       # a swap for a left-out option


class LeftOut(BaseModel):
    name: str
    reason: str


class Step(BaseModel):
    id: str
    label: str
    options: list[Option]
    left_out: list[LeftOut] = Field(default_factory=list)


class DishPlan(BaseModel):
    dish: str
    cuisine: str = "american"
    source: Literal["catalog", "template", "model"]
    steps: list[Step]

    def usual(self) -> list[Option]:
        return [o for s in self.steps for o in s.options if o.usual]

    def option(self, name: str) -> Option | None:
        return next((o for s in self.steps for o in s.options if o.name == name), None)


class PlanError(ValueError):
    """A model reply that can't be turned into a plan."""


@cache
def _yaml(name: str) -> dict[str, Any]:
    path = CONFIG / name
    return yaml.safe_load(path.read_text()) if path.is_file() else {}


def variants() -> dict[str, dict[str, Any]]:
    return _yaml("variants.yaml").get("variants") or {}


def templates() -> dict[str, dict[str, Any]]:
    return _yaml("dish_templates.yaml").get("templates") or {}


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def find_template(query: str) -> tuple[str, dict[str, Any]] | None:
    key = _key(query)
    for name, template in templates().items():
        if key == name or key in [_key(n) for n in template.get("names") or []]:
            return name, template
    return None


def _screened(step_id: str, label: str, options: list[Option], avoid: list[str], diet: str,
              screen: Screen) -> Step | None:
    kept: list[Option] = []
    left: list[LeftOut] = []
    for option in options:
        found = screen.check(option.name, avoid, diet)
        if found is None:
            kept.append(option)
            continue
        left.append(LeftOut(name=option.name, reason=found.reason))
        if found.swap:
            kept.append(Option(name=found.swap, category=option.category, usual=option.usual,
                               instead_of=option.name))
    unique: dict[str, Option] = {}
    for option in kept:
        unique.setdefault(option.name, option)
    kept = list(unique.values())
    # usual ones first, then the step's own options before swaps, the most common first
    kept.sort(key=lambda o: (not o.usual, o.instead_of is not None, -(o.share or 0)))
    if not kept and not left:
        return None
    return Step(id=step_id, label=label, options=kept[:MAX_OPTIONS], left_out=left)


def plan_from_ingredients(dish: str, cuisine: str, ingredients: list[dict[str, Any]],
                          avoid: list[str], diet: str, screen: Screen) -> DishPlan:
    """A catalog dish's plan. ingredients: {name, role, category, share?}."""
    kinds = variants()
    by_group: dict[str, list[Option]] = {g[0]: [] for g in GROUPS}
    for ing in ingredients:
        share = ing.get("share")
        usual = ing["role"] == "core" or (share is not None and share >= USUAL_SHARE)
        group = next(g for g, _, cats in GROUPS if ing["category"] in cats or g == "pantry")
        variant = kinds.get(ing["name"])
        if variant:
            for kind in variant["kinds"]:
                is_default = kind == variant["default"]
                by_group[group].append(Option(
                    name=kind, category=ing["category"], usual=usual and is_default,
                    share=share if is_default else None))
        else:
            by_group[group].append(Option(name=ing["name"], category=ing["category"],
                                          usual=usual, share=share))
    steps = [step for gid, label, _ in GROUPS
             if (step := _screened(gid, label, by_group[gid], avoid, diet, screen))]
    return DishPlan(dish=dish, cuisine=cuisine, source="catalog", steps=steps[:MAX_STEPS])


def plan_from_steps(dish: str, cuisine: str, source: Literal["template", "model"],
                    steps: list[dict[str, Any]], avoid: list[str], diet: str,
                    screen: Screen) -> DishPlan:
    """A template's or the model's plan: steps of {label, options: [{name, category,
    usual}]}. Raises PlanError when nothing usable is left."""
    built: list[Step] = []
    for i, raw in enumerate(steps[:MAX_STEPS]):
        label = str(raw.get("label") or "").strip()[:40]
        options = []
        for o in raw.get("options") or []:
            name = _key(str(o.get("name") or ""))[:60]
            if not name:
                continue
            category = o.get("category") if o.get("category") in CATEGORIES else "other"
            options.append(Option(name=name, category=category, usual=bool(o.get("usual"))))
        if label and options:
            step = _screened(f"s{i + 1}", label, options[:MAX_OPTIONS * 2], avoid, diet,
                             screen)
            if step:
                built.append(step)
    if not any(s.options for s in built):
        raise PlanError(f"no usable options for {dish!r}")
    return DishPlan(dish=dish, cuisine=cuisine, source=source, steps=built)


def plan_from_template(query: str, avoid: list[str], diet: str,
                       screen: Screen) -> DishPlan | None:
    found = find_template(query)
    if found is None:
        return None
    name, template = found
    return plan_from_steps(name, template.get("cuisine") or "american", "template",
                           template["steps"], avoid, diet, screen)


# The model's reply (engines' plan()): the schema it must follow
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dish": {"type": "string"},
        "cuisine": {"type": "string", "enum": ["american", "european", "latin_american",
                                               "east_se_asian", "south_asian",
                                               "middle_eastern_african"]},
        "steps": {
            "type": "array", "minItems": 1, "maxItems": MAX_STEPS,
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "options": {
                        "type": "array", "minItems": 1, "maxItems": MAX_OPTIONS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "category": {"type": "string", "enum": sorted(CATEGORIES)},
                                "usual": {"type": "boolean"},
                            },
                            "required": ["name", "category", "usual"],
                        },
                    },
                },
                "required": ["label", "options"],
            },
        },
    },
    "required": ["dish", "cuisine", "steps"],
}


def build_plan(query: str, avoid: list[str], diet: str, screen: Screen, *,
               catalog: Any, model: Any = None) -> DishPlan:
    """The plan for a dish, fastest source first: a template (a kind of food), then the
    catalog, then the model. catalog(query) -> (name, cuisine, ingredients) or None;
    model(query) -> {"dish", "cuisine", "steps"} (raises on failure), or None when the
    engine has no model. Raises PlanError when no source knows the dish."""
    plan = plan_from_template(query, avoid, diet, screen)
    if plan is not None:
        return plan
    found = catalog(query)
    if found is not None:
        name, cuisine, ingredients = found
        return plan_from_ingredients(name, cuisine, ingredients, avoid, diet, screen)
    if model is None:
        raise PlanError(f"no plan for {query!r}")
    reply = model(query)
    return plan_from_steps(_key(reply.get("dish") or query), reply.get("cuisine") or "american",
                           "model", reply.get("steps") or [], avoid, diet, screen)


def known_foods() -> set[str]:
    """Every food name the templates, variants and A3's substitutes table know (for the
    steps check: A3 tends to slip in substitutes such as "coconut cream")."""
    from app.engines.demo import SubstituteTable

    names: set[str] = set()
    for key, candidates in SubstituteTable().table.items():
        names.add(key)
        names.update(c["name"] for c in candidates)
    for template in templates().values():
        for step in template.get("steps") or []:
            names.update(o["name"] for o in step.get("options") or [])
    for item, variant in variants().items():
        names.add(item)
        names.update(variant.get("kinds") or [])
    return names
