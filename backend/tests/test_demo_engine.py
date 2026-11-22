import pytest

from app.engines.base import EngineError
from app.engines.demo import DemoCatalog, DemoEngine, SubstituteTable, read_chat
from pantrypath_core.result import Result

CATALOG = DemoCatalog()


def read(*texts: str):
    return read_chat([{"role": "user", "content": t} for t in texts], CATALOG)


@pytest.mark.parametrize("text, dish", [
    ("I feel like quiche tonight", "quiche lorraine"),
    ("pad thai please", "pad thai"),
    ("vegan pancakes for 2", "vegan pancakes"),
    ("some tikka masala", "chicken tikka masala"),
    ("something with chicken", None),
])
def test_dishes(text, dish):
    assert read(text).query == dish


def test_allergens_need_a_cue_and_a_dishs_own_words_dont_count():
    o = read("peanut butter cookies, no dairy or eggs, 45202")
    assert (o.query, o.avoid, o.zip) == ("peanut butter cookies", ["milk", "egg"], "45202")


def test_cues_conditions_diets_needs_and_servings():
    o = read("pad thai for 4, shellfish allergy, gluten-free, I'm celiac and vegetarian, halal")
    assert o.avoid == ["shellfish", "gluten"] and o.diet == "vegetarian"
    assert o.extras["servings"] == 4 and o.extras["unsupported"] == ["halal"]


def test_a_later_fine_withdraws_an_earlier_avoid():
    o = read("banana bread, no tree nuts", "actually tree nuts are fine")
    assert o.avoid == [] and o.mentioned == ["tree_nuts"]


def test_nuts_asks_about_peanuts_like_a1():
    o = read("brownies, I'm allergic to nuts")
    assert o.avoid == ["tree_nuts", "peanuts"] and o.chat_avoid == ["tree_nuts"]
    assert o.clarify == ["peanuts"] and [i.kind for i in o.issues] == ["clarify"]


REQUEST = {"query": "quiche lorraine", "zip": "45202", "avoid": ["milk", "gluten"],
           "diet": "vegetarian"}


def test_search_returns_a_valid_result_and_a_grounded_list():
    out = DemoEngine(delay_s=0).search(REQUEST, {"servings": 4, "unsupported": ["kosher"]}, [],
                                       lambda stage: None)
    Result.model_validate(out.result)   # the result.json contract
    products = {(i["name"], p["name"]): p for i in out.result["ingredients"]
                for p in i["products"]}
    for item in out.answer["ingredients"]:
        if item["status"] == "buy":
            assert products[(item["name"], item["product"])]["verdict"] == "safe"
    assert [i["name"] for i in out.answer["ingredients"]] == \
        [i["name"] for i in out.result["ingredients"]]
    assert any("kosher" in w for w in out.warnings)
    assert out.warnings[-1] == "Product data: Open Food Facts (openfoodfacts.org)"


def test_regular_products_break_the_diet_rules_they_should():
    out = DemoEngine(delay_s=0).search(REQUEST, {}, [], lambda stage: None)
    verdicts = {(i["name"], p["brand"]): p["verdict"] for i in out.result["ingredients"]
                for p in i["products"] if p["brand"] != "Greenway"
                and "(store brand)" not in p["name"]}
    for (name, _), verdict in verdicts.items():
        if name in ("bacon", "swiss cheese", "pie crust", "heavy cream", "half-and-half"):
            assert verdict == "unsafe", name


def test_no_free_from_version_of_the_allergen_itself():
    request = {"query": "pad thai", "zip": "45202", "avoid": ["peanuts", "egg", "shellfish"],
               "diet": "none"}
    for zip_code in ("45202", "10001", "60601", "94103"):   # different synthetic draws
        out = DemoEngine(delay_s=0).search({**request, "zip": zip_code}, {}, [],
                                           lambda s: None)
        names = [p["name"] for i in out.result["ingredients"] for p in i["products"]]
        assert not [n for n in names if "Peanut-Free Peanut" in n or "Egg-Free Egg" in n
                    or "Plant-Based Shrimp" in n]


def test_search_is_the_same_for_the_same_request():
    engine = DemoEngine(delay_s=0)
    a = engine.search(REQUEST, {}, [], lambda s: None).result["ingredients"]
    b = engine.search(REQUEST, {}, [], lambda s: None).result["ingredients"]
    assert a == b


def test_unknown_dish():
    with pytest.raises(EngineError) as err:
        DemoEngine(delay_s=0).search({**REQUEST, "query": "unicorn stew"}, {}, [],
                                     lambda s: None)
    assert err.value.kind == "not_found"


def test_substitutes_fit_the_shopper():
    table = SubstituteTable()
    assert table.pick("whole milk", ["gluten", "soy"], "none") == "rice milk"
    assert table.pick("butter", ["milk"], "vegan") == "vegan butter"
    assert table.pick("butter", ["milk", "soy"], "none") == "olive oil"  # vegan butter has soy
    assert table.pick("onion", ["milk"], "none") is None
