"""One guided build's moves (app/guided.py), without the chat or a server."""

import pytest

from app import guided
from app.plans import plan_from_template
from app.screen import Screen

SCREEN = Screen()
DRAFT = {"request": {"query": "sandwich", "zip": "45202", "avoid": ["peanuts"],
                     "diet": "vegetarian"}}


def build():
    plan = plan_from_template("sandwich", ["peanuts"], "vegetarian", SCREEN)
    return guided.new_build(plan, DRAFT)


@pytest.mark.parametrize(("text", "adds", "removes"), [
    ("pineapple", ["pineapple"], []),
    ("add pineapple and Dave's Killer Bread", ["pineapple", "Dave's Killer Bread"], []),
    ("also some jalapeños, no onions", ["jalapeños"], ["onions"]),
    ("remove the mayo", [], ["mayo"]),
    ("  ,  and ", [], []),
])
def test_reading_typed_items(text, adds, removes):
    assert guided.read_items(text) == (adds, removes)


def test_a_build_starts_with_the_usual_picks_and_moves_step_by_step():
    b = build()
    assert b["picks"]["s1"] == ["white bread"] and b["state"] == "choosing"
    ticked = guided.move(b, "next", ["sourdough bread", "not an option"])
    assert ticked == ["sourdough bread"] and b["picks"]["s1"] == ["sourdough bread"]
    assert b["step"] == 1
    guided.move(b, "back")
    assert b["step"] == 0
    guided.move(b, "finish")
    assert b["state"] == "done"
    with pytest.raises(guided.BuildError):
        guided.move(b, "next")


def test_additions_are_checked_and_clashes_wait_for_the_shopper():
    b = build()
    passed = guided.add_items(b, "add pineapple, peanut butter and Dave's Killer Bread", SCREEN)
    assert passed == ["pineapple", "Dave's Killer Bread"]
    assert guided.pending_clashes(b) == ["peanut butter"]
    clash = next(a for a in b["additions"] if a["name"] == "peanut butter")["clash"]
    assert clash["reason"] == "contains peanuts, which you avoid" and clash["swap"]
    assert guided.resolve(b, "peanut butter", "swap") == [clash["swap"]]
    assert guided.pending_clashes(b) == []
    with pytest.raises(guided.BuildError):
        guided.resolve(b, "pineapple", "anyway")


def test_search_anyway_keeps_the_item_and_remove_drops_it():
    b = build()
    guided.add_items(b, "bacon, peanut butter", SCREEN)
    assert guided.resolve(b, "bacon", "anyway") == ["bacon"]
    assert guided.resolve(b, "peanut butter", "remove") == []
    assert [a["name"] for a in b["additions"]] == ["bacon"]
    assert "bacon" in [i["name"] for i in guided.chosen(b)]


def test_typing_an_offered_option_ticks_it_and_no_x_unticks_it():
    b = build()
    assert guided.add_items(b, "add avocado", SCREEN) == ["avocado"]
    assert "avocado" in b["picks"]["s4"] and b["additions"] == []
    guided.add_items(b, "no tomatoes, hold the mayo", SCREEN)
    assert "tomatoes" not in b["picks"]["s4"] and b["picks"]["s5"] == []


def test_the_chosen_list_and_use_the_usual_for_the_rest():
    b = build()
    guided.move(b, "next", ["rye bread"])
    guided.add_items(b, "pineapple", SCREEN)
    names = [i["name"] for i in guided.chosen(b, usual_for_the_rest=True)]
    assert names[0] == "rye bread" and names[-1] == "pineapple"
    assert {"cheddar cheese", "lettuce", "tomatoes", "mayonnaise"} <= set(names)
    pineapple = guided.chosen(b)[-1]
    assert pineapple == {"name": "pineapple", "category": "other", "role": "common"}


def test_cancel_words():
    assert guided.is_cancel("start over") and guided.is_cancel("Never mind!")
    assert not guided.is_cancel("no onions")
