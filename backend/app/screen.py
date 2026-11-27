"""Does an ingredient or product *name* clash with the shopper's allergens or diet?

Used before anything is searched: to leave clashing options out of a guided build
(plans.py) and to warn about an item the shopper adds (guided.py). It runs the real diet
rules (diet_rules.detect_allergens / classify_diet) on the name plus what that food usually
contains (the demo's composition table, and a few more below), so "mayonnaise" counts as
egg and "white bread" as wheat. It only sees names: every product found later is still
judged on its own label, so a name that passes here can still be "not safe" there.
"""

from dataclasses import dataclass, field

from app.engines.demo import SubstituteTable, composition
from app.vocab import allergen_label
from pantrypath_core.diet_rules import (
    DietClass,
    ProductEvidence,
    classify_diet,
    detect_allergens,
)

# Usual contents the demo's table doesn't have (lower-case name -> ingredient text)
USUALLY = {
    "pesto": "basil, olive oil, parmesan cheese, pine nuts, garlic",
    "basil pesto": "basil, olive oil, parmesan cheese, pine nuts, garlic",
    "ranch dressing": "soybean oil, buttermilk, egg yolk, vinegar, garlic, herbs",
    "caesar dressing": "soybean oil, parmesan cheese, egg yolk, anchovies, lemon, garlic",
    "aioli": "olive oil, egg yolk, garlic, lemon",
    "tzatziki": "yogurt, cucumber, garlic, dill",
    "naan": "wheat flour, milk, yogurt, butter",
    "brioche": "wheat flour, eggs, butter, milk, sugar",
}
# What each diet rules out, as the classes classify_diet returns
DIET_BLOCKS = {"none": set(),
               "vegetarian": {DietClass.non_vegetarian},
               "vegan": {DietClass.non_vegetarian, DietClass.vegetarian}}


@dataclass
class Clash:
    name: str
    allergens: list[str] = field(default_factory=list)   # avoided allergens it contains
    diet: str | None = None        # the diet it breaks ("vegetarian"), if any
    swap: str | None = None        # a substitute that passes, if the table has one

    @property
    def reason(self) -> str:
        """'contains peanuts, which you avoid' / 'isn't vegetarian'."""
        parts = []
        if self.allergens:
            words = [allergen_label(a).lower() for a in self.allergens]
            parts.append(f"contains {' and '.join(words)}, which you avoid")
        if self.diet:
            parts.append(f"isn't {self.diet}")
        return " and ".join(parts)

    def view(self) -> dict:
        return {"name": self.name, "allergens": self.allergens, "diet": self.diet,
                "swap": self.swap, "reason": self.reason}


def usual_text(name: str) -> str:
    key = name.strip().lower()
    return USUALLY.get(key) or composition(key)


def clash(name: str, avoid: list[str], diet: str) -> Clash | None:
    """The clash of a name with the avoid list and diet, or None if the name passes."""
    text = usual_text(name)
    evidence = ProductEvidence(source="off", name=name, ingredients_text=text)
    contains, _ = detect_allergens(evidence)
    found = sorted(a.value for a in contains if a.value in set(avoid))
    kind, _ = classify_diet(evidence)
    broken = diet if kind in DIET_BLOCKS.get(diet, set()) else None
    if not found and not broken:
        return None
    return Clash(name=name, allergens=found, diet=broken)


class Screen:
    """Name checks with swaps from ml/configs/substitutes.yaml (A3's table)."""

    def __init__(self, table: SubstituteTable | None = None):
        self.table = table or SubstituteTable()

    def check(self, name: str, avoid: list[str], diet: str) -> Clash | None:
        found = clash(name, avoid, diet)
        if found is not None:
            swap = self.table.pick(name, avoid, diet)
            # the table's own screen uses its `contains` lists; check the name too
            if swap and clash(swap, avoid, diet) is None:
                found.swap = swap
        return found
