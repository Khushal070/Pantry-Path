import yaml

from app.settings import REPO_ROOT
from app.vocab import ALLERGENS, NEEDS, join_words, ordered_allergens, stricter_diet
from pantrypath_core.diet_rules import Allergen


def test_needs_match_the_parsers_unsupported_labels():
    config = yaml.safe_load((REPO_ROOT / "ml" / "configs" / "parse_request.yaml").read_text())
    assert NEEDS == [n for n in config["unsupported_labels"] if n != "other"]


def test_allergens_follow_the_contract():
    assert ALLERGENS == [a.value for a in Allergen]
    assert ordered_allergens(["gluten", "milk", "milk", "nope"]) == ["milk", "gluten"]


def test_helpers():
    assert stricter_diet("vegetarian", "vegan", None) == "vegan"
    assert stricter_diet(None, "none") == "none"
    assert join_words(["milk"]) == "milk"
    assert join_words(["milk", "egg", "soy"]) == "milk, egg and soy"
