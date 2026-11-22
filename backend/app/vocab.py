"""Names and labels shared by the API and the UI: allergens, diets, and needs we can't check.

Allergens and diets come from pantrypath_core.diet_rules, so the profile, the chat and
the diet rules all use the same values.
"""

from collections.abc import Iterable

from pantrypath_core.diet_rules import Allergen, Diet

ALLERGENS: list[str] = [a.value for a in Allergen]  # the contract's order
DIETS: list[str] = [Diet.none.value, Diet.vegetarian.value, Diet.vegan.value]
DIET_RANK = {Diet.none.value: 0, Diet.vegetarian.value: 1, Diet.vegan.value: 2}

ALLERGEN_LABELS = {
    "milk": "Milk", "egg": "Egg", "fish": "Fish", "shellfish": "Shellfish",
    "tree_nuts": "Tree nuts", "peanuts": "Peanuts", "wheat": "Wheat", "soy": "Soy",
    "sesame": "Sesame", "gluten": "Gluten",
}
DIET_LABELS = {"none": "No diet", "vegetarian": "Vegetarian", "vegan": "Vegan"}

# Needs A1 flags as unsupported (ml/configs/parse_request.yaml unsupported_labels, without
# "other"): diet_rules can't check them yet, so the answer warns about them. A profile
# can save them too, and the same warning appears. tests/test_vocab.py keeps the list in
# step with the YAML.
NEEDS: list[str] = [
    "halal", "kosher", "low_sodium", "diabetic", "pregnancy", "keto", "paleo", "low_carb",
    "low_fat", "low_fodmap", "pescatarian", "mustard_allergy", "celery_allergy",
    "corn_allergy", "sulfite_allergy",
]
NEED_LABELS = {n: n.replace("_", " ").capitalize() for n in NEEDS} | {
    "low_fodmap": "Low FODMAP",
}


def ordered_allergens(values: Iterable[str]) -> list[str]:
    """Known allergens only, once each, in the contract's order."""
    wanted = set(values)
    return [a for a in ALLERGENS if a in wanted]


def stricter_diet(*diets: str | None) -> str:
    return max((d or "none" for d in diets), key=DIET_RANK.__getitem__, default="none")


def allergen_label(allergen: str) -> str:
    return ALLERGEN_LABELS.get(allergen, allergen.replace("_", " ")).lower()


def join_words(words: list[str]) -> str:
    """'milk', 'milk and egg', 'milk, egg and soy'."""
    if len(words) <= 2:
        return " and ".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]
