"""Steps that match the final list (app/stepcheck.py)."""

from app.stepcheck import correction, is_allowed, repair, strays

VOCAB = ["swiss cheese", "cheddar cheese", "coconut cream", "cream", "milk", "eggs",
         "bacon", "smoked mushrooms", "pie crust", "onion", "cheese", "butter"]
ALLOWED = ["pie crust", "eggs", "milk", "onion", "cheddar cheese", "smoked mushrooms"]


def test_part_of_a_listed_item_and_basics_are_allowed():
    assert is_allowed("cheese", ALLOWED) and is_allowed("egg", ALLOWED)
    assert is_allowed("salt", []) and not is_allowed("swiss cheese", ALLOWED)


def test_strays_are_found_longest_first():
    assert strays("Beat the eggs and stir in coconut cream.", ALLOWED, VOCAB) == \
        ["coconut cream"]
    assert strays("Top with Swiss cheese.", ALLOWED, VOCAB) == ["swiss cheese"]
    assert strays("Pour the egg and milk mixture into the pie crust.", ALLOWED, VOCAB) == []


def test_a_swapped_item_may_be_named_in_a_swap_phrase_only():
    assert strays("Use smoked mushrooms instead of bacon.", ALLOWED, VOCAB) == []
    assert strays("Fry the bacon.", ALLOWED, VOCAB) == ["bacon"]


def test_repair_replaces_unchosen_options_and_drops_other_strays():
    steps = ["Preheat the oven.", "Top with Swiss cheese.",
             "Stir in coconut cream.", "Bake 35 minutes."]
    fixed = repair(steps, ALLOWED, VOCAB, {"swiss cheese": "cheddar cheese"})
    assert fixed == ["Preheat the oven.", "Top with cheddar cheese.", "Bake 35 minutes."]


def test_the_correction_names_the_list_and_the_strays():
    text = correction(["eggs", "milk"], ["coconut cream", "coconut cream"])
    assert text == ("In the steps and summary use only these ingredients: eggs, milk. "
                    "Do not mention coconut cream.")


def test_the_food_vocabulary_knows_the_substitutes_table():
    from app.plans import known_foods

    foods = known_foods()
    assert {"coconut cream", "oat milk"} <= foods
    assert strays("Stir in the coconut cream.", ["white bread", "mayonnaise"], foods) == \
        ["coconut cream"]
