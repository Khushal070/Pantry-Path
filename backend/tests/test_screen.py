"""Name checks before anything is searched (app/screen.py): the real diet rules on a name
and what that food usually contains."""

import pytest

from app.screen import Screen, clash


@pytest.mark.parametrize(("name", "avoid", "diet", "allergens", "broken"), [
    ("peanut butter", ["peanuts"], "none", ["peanuts"], None),
    ("bacon", [], "vegetarian", [], "vegetarian"),
    ("cheddar cheese", [], "vegan", [], "vegan"),
    ("cheddar cheese", ["milk"], "none", ["milk"], None),
    ("mayonnaise", ["egg"], "none", ["egg"], None),          # from what it usually contains
    ("white bread", ["wheat"], "none", ["wheat"], None),
    ("worcestershire sauce", [], "vegetarian", [], "vegetarian"),
    ("pesto", ["tree_nuts", "milk"], "none", ["milk", "tree_nuts"], None),
    ("shrimp", ["shellfish"], "vegetarian", ["shellfish"], "vegetarian"),
])
def test_clashes(name, avoid, diet, allergens, broken):
    found = clash(name, avoid, diet)
    assert found is not None
    assert found.allergens == allergens and found.diet == broken


@pytest.mark.parametrize(("name", "avoid", "diet"), [
    ("pineapple", ["peanuts"], "vegan"),
    ("cheddar cheese", ["peanuts"], "vegetarian"),     # dairy is vegetarian
    ("Dave's Killer Bread", ["peanuts"], "vegan"),     # a product name passes on its name
    ("tofu", [], "vegan"),
    ("hummus", ["milk"], "vegan"),
])
def test_passes(name, avoid, diet):
    assert clash(name, avoid, diet) is None


def test_reason_reads_well():
    assert clash("peanut butter", ["peanuts"], "none").reason == \
        "contains peanuts, which you avoid"
    assert clash("bacon", [], "vegetarian").reason == "isn't vegetarian"


def test_a_swap_is_offered_only_when_it_passes():
    screen = Screen()
    milk = screen.check("milk", ["milk"], "none")
    assert milk.swap is not None and clash(milk.swap, ["milk"], "none") is None
    found = screen.check("peanut butter", ["peanuts"], "none")
    assert found.swap is None or clash(found.swap, ["peanuts"], "none") is None
