"""Dish plans (app/plans.py): catalog grouping, variants, templates, model replies, and
options screened against the shopper's allergens and diet."""

import pytest

from app.plans import (
    MAX_OPTIONS,
    PlanError,
    find_template,
    plan_from_ingredients,
    plan_from_steps,
    plan_from_template,
)
from app.screen import Screen

SCREEN = Screen()
TUNA_SANDWICH = [  # the real catalog's tuna salad sandwich, shortened
    {"name": "mayonnaise", "role": "core", "category": "condiment", "share": 0.5},
    {"name": "canned tuna", "role": "core", "category": "seafood", "share": 0.44},
    {"name": "sandwich bread", "role": "core", "category": "bakery", "share": 0.0},
    {"name": "celery", "role": "common", "category": "produce", "share": 0.5},
    {"name": "dijon mustard", "role": "common", "category": "condiment", "share": 0.38},
    {"name": "lettuce", "role": "common", "category": "produce", "share": 0.25},
    {"name": "fresh dill", "role": "common", "category": "spice_herb", "share": 0.12},
]


def step(plan, label):
    return next(s for s in plan.steps if s.label == label)


def names(step_):
    return [o.name for o in step_.options]


def test_catalog_ingredients_become_steps_in_order():
    plan = plan_from_ingredients("tuna salad sandwich", "american", TUNA_SANDWICH, [], "none",
                                 SCREEN)
    assert plan.source == "catalog"
    assert [s.label for s in plan.steps] == ["Bread and grains", "Protein",
                                             "Vegetables and fruit", "Sauces", "Pantry basics"]
    assert {o.name for o in plan.usual()} == {"mayonnaise", "canned tuna", "white bread",
                                              "celery"}   # core, or share >= 0.5
    assert step(plan, "Sauces").options[0].share == 0.5


def test_a_generic_ingredient_becomes_its_kinds():
    plan = plan_from_ingredients("tuna salad sandwich", "american", TUNA_SANDWICH, [], "none",
                                 SCREEN)
    bread = step(plan, "Bread and grains")
    assert names(bread)[0] == "white bread" and bread.options[0].usual
    assert {"sourdough bread", "rye bread"} <= set(names(bread))
    assert not any(o.usual for o in bread.options[1:])
    lettuce_kinds = names(step(plan, "Vegetables and fruit"))
    assert "romaine lettuce" in lettuce_kinds and "lettuce" not in lettuce_kinds


def test_clashing_options_are_left_out_with_the_reason_and_a_swap():
    plan = plan_from_ingredients("tuna salad sandwich", "american", TUNA_SANDWICH, ["egg"],
                                 "vegetarian", SCREEN)
    assert "canned tuna" not in [o.name for o in plan.usual()]
    protein = step(plan, "Protein")
    assert [x.name for x in protein.left_out] == ["canned tuna"]
    assert "isn't vegetarian" in protein.left_out[0].reason
    sauces = step(plan, "Sauces")
    assert "mayonnaise" in [x.name for x in sauces.left_out]
    swap = next(o for o in sauces.options if o.instead_of == "mayonnaise")
    assert swap.usual    # mayonnaise was usual, so its swap starts ticked


def test_a_kind_of_food_gets_a_template():
    assert find_template("Sandwich")[0] == "sandwich"
    assert find_template("hoagie")[0] == "sandwich"
    assert find_template("tuna salad sandwich") is None
    plan = plan_from_template("sandwich", ["peanuts"], "vegetarian", SCREEN)
    assert plan.source == "template" and plan.dish == "sandwich"
    assert [s.label for s in plan.steps][:2] == ["Bread", "Protein"]
    protein = step(plan, "Protein")
    assert "sliced turkey" in [x.name for x in protein.left_out]
    assert "tofu" in names(protein) and "ham" not in names(protein)
    assert all(len(s.options) <= MAX_OPTIONS for s in plan.steps)


def test_a_model_reply_is_cleaned_and_screened():
    reply = [{"label": "Base", "options": [
                {"name": "  Rice ", "category": "grain", "usual": True},
                {"name": "", "category": "grain", "usual": False},
                {"name": "peanut sauce", "category": "spaceship", "usual": True}]},
             {"label": "", "options": [{"name": "x", "category": "other", "usual": True}]}]
    plan = plan_from_steps("bowl", "east_se_asian", "model", reply, ["peanuts"], "none", SCREEN)
    assert [s.label for s in plan.steps] == ["Base"]
    assert names(plan.steps[0]) == ["rice"]
    assert plan.steps[0].left_out[0].name == "peanut sauce"


def test_a_model_reply_with_nothing_usable_is_an_error():
    with pytest.raises(PlanError):
        plan_from_steps("x", "american", "model",
                        [{"label": "Meat", "options": [{"name": "lamb chops", "category": "meat",
                                                        "usual": True}]}],
                        [], "vegan", SCREEN)
