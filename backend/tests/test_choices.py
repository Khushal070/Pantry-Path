"""Product choices (app/choices.py): main store, picks, favourites, family hints."""

from app.choices import choice_for, family, find_view, main_store, search_terms

STORES = [{"store_id": "A", "name": "Kroger A", "distance_miles": 0.5},
          {"store_id": "B", "name": "Kroger B", "distance_miles": 1.2}]


def p(name, store, verdict="safe", brand=""):
    return {"name": name, "brand": brand, "store_id": store, "verdict": verdict,
            "reason": "r", "aisle": "AISLE 1", "size": "1 lb", "barcode": None}


FOUND = {"stores": STORES, "ingredients": [
    {"name": "white bread", "products": [p("Wonder White", "A"), p("Kroger White", "B")]},
    {"name": "cheddar cheese", "products": [p("Store Cheddar", "A", "unsafe"),
                                            p("Tillamook Cheddar", "B", brand="Tillamook")]},
    {"name": "swiss cheese", "products": [p("Generic Swiss", "A"),
                                          p("Tillamook Swiss", "A", brand="Tillamook")]},
    {"name": "Tillamook swiss cheese", "products": [p("Tillamook Swiss", "A",
                                                      brand="Tillamook")]},
    {"name": "pickles", "products": [p("Dill Pickles", "B", "unknown"),
                                     p("Spicy Pickles", "B", "unsafe")]},
    {"name": "Dave's Killer Bread sandwich bread", "products": [
        p("Dave's Killer Bread 21 Grains", "B", brand="Dave's Killer Bread")]},
    {"name": "sandwich bread", "products": [p("Plain Sandwich", "A")]},
]}


def test_family():
    assert family("swiss cheese") == "cheese" and family("Tortillas") == "tortilla"
    assert family("hummus") == "hummus"


def test_the_main_store_has_safe_products_for_most_usual_items():
    assert main_store(FOUND, ["white bread", "swiss cheese"]) == "A"
    assert main_store(FOUND, ["cheddar cheese"]) == "B"
    assert main_store({"stores": [], "ingredients": []}, []) is None


def test_the_pick_is_safe_at_the_main_store_first_and_never_unsafe():
    choice = choice_for("white bread", FOUND, "A")
    assert choice["pick"]["name"] == "Wonder White" and not choice["pick"]["other_store"]
    assert [a["name"] for a in choice["alternatives"]] == ["Kroger White"]
    cheddar = choice_for("cheddar cheese", FOUND, "A")
    assert cheddar["pick"]["name"] == "Tillamook Cheddar" and cheddar["pick"]["other_store"]
    assert cheddar["hidden_unsafe"] == 1 and cheddar["alternatives"] == []


def test_read_the_label_when_nothing_is_safe():
    choice = choice_for("pickles", FOUND, "A")
    assert choice["pick"]["verdict"] == "unknown" and choice["hidden_unsafe"] == 1


def test_a_favourite_is_picked_even_at_another_store():
    extra = search_terms(["sandwich bread"], {"sandwich bread": "Dave's Killer Bread"})
    choice = choice_for("sandwich bread", FOUND, "A", extra["sandwich bread"])
    assert choice["pick"]["name"] == "Dave's Killer Bread 21 Grains"
    assert choice["favourite"] == "Dave's Killer Bread" and not choice["favourite_missing"]


def test_a_missing_favourite_says_so_and_picks_as_usual():
    extra = search_terms(["white bread"], {"white bread": "Nature's Own"})
    choice = choice_for("white bread", FOUND, "A", extra["white bread"])
    assert choice["favourite_missing"] and choice["pick"]["name"] == "Wonder White"


def test_a_family_brand_is_offered_first_but_not_picked():
    extra = search_terms(["swiss cheese"], {"cheddar cheese": "Tillamook"})
    assert extra["swiss cheese"]["hint"] == "Tillamook swiss cheese"
    choice = choice_for("swiss cheese", FOUND, "A", extra["swiss cheese"])
    assert choice["hint"]["name"] == "Tillamook Swiss"
    assert choice["pick"]["name"] == "Generic Swiss"
    assert "Tillamook Swiss" not in [a["name"] for a in choice["alternatives"]]
    assert find_view(choice, choice["hint"]["key"])["name"] == "Tillamook Swiss"
