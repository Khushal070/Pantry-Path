"""Demo engine: the whole chat offline, with no model server, store keys or DVC data.

Use it to build and show the frontend anywhere. Nothing in it is shopping advice.

- parse: a small keyword reader stands in for A1. It knows the 70 dishes of
  demo_catalog.json, allergen and diet words after cues ("no dairy", "nut allergy",
  "gluten-free"), a ZIP and servings. A1 with its cross-check is far stronger.
- search: three synthetic stores and synthetic products for the dish's catalog
  ingredients. Every product is judged by the real diet rules
  (pantrypath_core.diet_rules.assess), and the result is validated against the
  result.json contract, so the frontend sees exactly the shape live mode returns.
- answer: the shopping list is decided by code the way A3's hybrid does (the nearest
  safe product, else a label check, else a substitute from ml/configs/substitutes.yaml,
  else skip). The summary is templated and there are no steps: the model writes those
  in live mode.
"""

import json
import random
import re
import time
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from app.engines.base import EngineError, Issue, ParseOutcome, Progress, SearchOutcome
from app.settings import REPO_ROOT
from app.vocab import DIET_RANK, allergen_label, join_words, ordered_allergens
from pantrypath_core.diet_rules import ProductEvidence, assess
from pantrypath_core.result import (
    DishInfo,
    IngredientResult,
    Meta,
    Product,
    Result,
    Store,
    Summary,
)

CATALOG_FILE = Path(__file__).with_name("demo_catalog.json")
SUBSTITUTES_FILE = REPO_ROOT / "ml" / "configs" / "substitutes.yaml"
ANSWER_CONFIG_FILE = REPO_ROOT / "ml" / "configs" / "answer.yaml"

# ---------------------------------------------------------------- the chat reader

# word -> allergens. A sketch of A1's cross-check word table (ml/configs/parse_request.yaml).
TERMS: dict[str, list[str]] = {
    "milk": ["milk"], "dairy": ["milk"], "lactose": ["milk"], "cheese": ["milk"],
    "butter": ["milk"], "cream": ["milk"], "egg": ["egg"], "eggs": ["egg"],
    "fish": ["fish"], "seafood": ["fish", "shellfish"], "shellfish": ["shellfish"],
    "shrimp": ["shellfish"], "prawns": ["shellfish"], "crab": ["shellfish"],
    "lobster": ["shellfish"], "tree nut": ["tree_nuts"], "tree nuts": ["tree_nuts"],
    "almond": ["tree_nuts"], "almonds": ["tree_nuts"], "cashew": ["tree_nuts"],
    "cashews": ["tree_nuts"], "walnut": ["tree_nuts"], "walnuts": ["tree_nuts"],
    "pecan": ["tree_nuts"], "pecans": ["tree_nuts"], "pistachio": ["tree_nuts"],
    "pistachios": ["tree_nuts"], "hazelnut": ["tree_nuts"], "hazelnuts": ["tree_nuts"],
    "nut": ["tree_nuts"], "nuts": ["tree_nuts"], "peanut": ["peanuts"],
    "peanuts": ["peanuts"], "peanut butter": ["peanuts"], "wheat": ["wheat"],
    "gluten": ["gluten"], "soy": ["soy"], "soya": ["soy"], "sesame": ["sesame"],
    "tahini": ["sesame"],
}
AMBIGUOUS_NUTS = {"nut", "nuts"}    # tree nuts, or peanuts too? (A1 asks the same)
CONDITIONS = {"celiac": ["gluten"], "coeliac": ["gluten"]}
NEEDS_WORDS = {
    "halal": "halal", "kosher": "kosher", "low sodium": "low_sodium",
    "low-sodium": "low_sodium", "low salt": "low_sodium", "diabetic": "diabetic",
    "diabetes": "diabetic", "pregnant": "pregnancy", "pregnancy": "pregnancy",
    "keto": "keto", "paleo": "paleo", "low carb": "low_carb", "low-carb": "low_carb",
    "low fat": "low_fat", "low-fat": "low_fat", "fodmap": "low_fodmap",
    "pescatarian": "pescatarian",
}

_TERM = "|".join(re.escape(t) for t in sorted(TERMS, key=len, reverse=True))
TERM_RE = re.compile(rf"\b(?:{_TERM})\b")
AVOID_BEFORE = re.compile(
    r"\b(?:no|not|without|avoid|avoiding|allergic to|allergy to|allergies to|"
    r"can't have|cannot have|can't eat|cannot eat|don't eat|do not eat|free of|skip|"
    r"hold the|nothing with)\b\s*")
AVOID_AFTER = re.compile(rf"\b({_TERM})(?:-|\s+)(?:free|allergy|allergies|intolerance|"
                         r"intolerant)\b")
FINE_AFTER = re.compile(rf"\b({_TERM})\s+(?:is|are)\s+(?:fine|ok|okay|alright)\b")
FINE_BEFORE = re.compile(r"\bcan (?:eat|have)\s+")
LIST_SEP = re.compile(r"\s*(?:,|&|/|\band\b|\bor\b|\bnor\b)\s*(?:(?:no|any|the|other)\s+)?")
LIST_FILLER = re.compile(r"(?:(?:any|the|all|some|other)\s+)?")
ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
SERVINGS_RES = [
    re.compile(r"\bfor (\d{1,2})\b(?!\s*(?:am|pm|minutes|mins|hours|dollars|bucks))"),
    re.compile(r"\b(\d{1,2})\s+(?:people|persons|servings|guests|of us|portions)\b"),
    re.compile(r"\bserves? (\d{1,2})\b"),
]


def _stem(token: str) -> str:
    token = token.replace("'", "")
    if len(token) > 4 and token.endswith("es") and token[-3] in "osxh":
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokens(text: str) -> list[str]:
    return [_stem(t) for t in re.findall(r"[a-z0-9']+", text.lower())]


class DemoCatalog:
    """The demo's dishes and a forgiving name match: the whole name, all of its words,
    or one word only that dish has ("quiche" -> quiche lorraine)."""

    GENERIC = {"chicken", "beef", "pork", "salad", "soup", "cake", "bread", "pie",
               "cookie", "sauce", "casserole", "with", "and", "the", "steak", "potato",
               "pasta", "egg", "cheese", "cream", "vegan", "taco", "spaghetti", "sweet",
               "sour", "white", "dark", "roll", "bean", "chocolate", "broccoli", "frosting",
               "stir", "fry", "fried", "southern", "general", "sandwich", "pot", "dinner",
               "breakfast", "chili", "baked", "banana", "peanut", "butter", "tuna", "shrimp",
               "chip", "noodle", "rib"}

    def __init__(self, path: Path = CATALOG_FILE):
        self.dishes: list[dict[str, Any]] = json.loads(path.read_text())["dishes"]
        self.by_name = {d["name"]: d for d in self.dishes}
        counts: dict[str, int] = {}
        for dish in self.dishes:
            for token in set(_tokens(dish["name"])):
                counts[token] = counts.get(token, 0) + 1
        self._distinct = {t for t, n in counts.items()
                          if n == 1 and len(t) >= 4 and t not in self.GENERIC}

    def match(self, text: str) -> dict[str, Any] | None:
        words = _tokens(text)
        joined = f" {' '.join(words)} "
        best, best_score = None, (-1, 0)
        for dish in self.dishes:
            name = _tokens(dish["name"])
            if f" {' '.join(name)} " in joined:
                score = (2, len(name))
            elif all(t in words for t in name):
                score = (1, len(name))
            elif any(t in self._distinct and t in words for t in name):
                score = (0, -len(name))
            else:
                continue
            if score > best_score:
                best, best_score = dish, score
        return best


def _list_terms(text: str, pos: int) -> list[re.Match[str]]:
    """'dairy, eggs or nuts': the allergen words in a list starting at pos."""
    pos = LIST_FILLER.match(text, pos).end()
    found = []
    while (term := TERM_RE.match(text, pos)) is not None:
        found.append(term)
        sep = LIST_SEP.match(text, term.end())
        if sep is None or sep.end() == term.end():
            break
        pos = sep.end()
    return found


def _template_dish(text: str) -> dict[str, Any] | None:
    """A kind of food with a build-your-own template ("sandwich", "a salad"): a dish for
    the demo's reader too, so guided builds work in demo mode (plans.py)."""
    from app.plans import templates  # plans -> screen -> this module

    for name, template in templates().items():
        for word in [name, *(template.get("names") or [])]:
            if re.search(rf"\b{re.escape(word)}s?\b", text):
                return {"name": name}
    return None


def _template_as_dish(query: str) -> dict[str, Any] | None:
    """A kind of food searched without a guided build: its template's usual options."""
    from app.plans import find_template

    found = find_template(query)
    if found is None:
        return None
    name, template = found
    return {"name": name, "cuisine": template.get("cuisine") or "american",
            "ingredients": [{"name": o["name"], "role": "core", "category": o["category"]}
                            for step in template["steps"] for o in step["options"]
                            if o.get("usual")]}


def read_chat(messages: list[dict[str, str]], catalog: DemoCatalog) -> ParseOutcome:
    """The demo's stand-in for A1: the shopper's messages in order, later ones win."""
    avoid: set[str] = set()
    mentioned: set[str] = set()
    unsupported: set[str] = set()
    clarify = False
    diet, zip_, servings, dish = "none", None, None, None
    for message in messages:
        if message["role"] != "user":
            continue
        text = message["content"].lower().replace("’", "'")
        if re.search(r"\bvegan\b", text):   # "vegan pancakes" too: before the dish is cut
            diet = "vegan"
        elif re.search(r"\b(?:vegetarian|veggie)\b", text) and DIET_RANK[diet] < 1:
            diet = "vegetarian"
        found = catalog.match(text) or _template_dish(text)
        if found:
            dish = found
            # a dish's own words ("peanut butter cookies") are not allergens to avoid
            text = text.replace(found["name"], " ")
        events: list[tuple[int, str, str]] = []   # (position, avoid|fine, word)
        for m in FINE_AFTER.finditer(text):
            events.append((m.start(1), "fine", m.group(1)))
        for cue in FINE_BEFORE.finditer(text):
            events += [(t.start(), "fine", t.group(0)) for t in _list_terms(text, cue.end())]
        fine_at = {pos for pos, kind, _ in events if kind == "fine"}
        for cue in AVOID_BEFORE.finditer(text):
            events += [(t.start(), "avoid", t.group(0)) for t in _list_terms(text, cue.end())
                       if t.start() not in fine_at]
        for m in AVOID_AFTER.finditer(text):
            events.append((m.start(1), "avoid", m.group(1)))
        for word, allergens in CONDITIONS.items():
            if re.search(rf"\b{word}\b", text):
                events.append((text.index(word), "avoid", word))
                mentioned.update(allergens)
        for _pos, kind, word in sorted(set(events)):
            allergens = TERMS.get(word) or CONDITIONS.get(word) or []
            mentioned.update(allergens)
            if kind == "avoid":
                avoid.update(allergens)
                clarify = clarify or word in AMBIGUOUS_NUTS
            else:
                avoid.difference_update(allergens)
        unsupported |= {label for word, label in NEEDS_WORDS.items()
                        if re.search(rf"\b{re.escape(word)}\b", text)}
        zips = ZIP_RE.findall(text)
        zip_ = zips[-1] if zips else zip_
        for pattern in SERVINGS_RES:
            if (m := pattern.search(text)) and 0 < int(m.group(1)) <= 24:
                servings = int(m.group(1))
    chat_avoid = ordered_allergens(avoid)
    issues, ask = [], []
    if clarify and "tree_nuts" in avoid and "peanuts" not in avoid:
        ask = ["peanuts"]   # like A1: the suggestion avoids both and asks
        issues.append(Issue("clarify", allergen="peanuts"))
    return ParseOutcome(
        status="ok", query=dish["name"] if dish else None, zip=zip_,
        avoid=ordered_allergens({*avoid, *ask}), diet=diet,
        extras={"servings": servings, "tweaks": [], "unsupported": sorted(unsupported)},
        issues=issues, mentioned=ordered_allergens(mentioned), chat_avoid=chat_avoid,
        clarify=ask, in_catalog=(dish["name"] in catalog.by_name) if dish else None,
    )


# ---------------------------------------------------------------- synthetic products

# How a few composite foods are made, so the real diet rules see their hidden
# allergens. Anything else is listed as its own name.
COMPOSITION = {
    "soy sauce": "water, soybeans, wheat, salt",
    "hoisin sauce": "sugar, water, soybeans, sesame seeds, wheat flour, garlic",
    "oyster sauce": "water, sugar, oyster extract, salt, wheat flour",
    "fish sauce": "anchovy extract, salt, sugar",
    "worcestershire sauce": "vinegar, molasses, anchovies, tamarind, garlic",
    "anchovy paste": "anchovies, olive oil, salt",
    "mayonnaise": "soybean oil, eggs, vinegar, salt",
    "cream of chicken soup": "chicken stock, wheat flour, cream, chicken, salt",
    "cream of mushroom soup": "water, mushrooms, wheat flour, cream, salt",
    "italian breadcrumbs": "wheat flour, salt, parmesan cheese, herbs",
    "italian dressing": "soybean oil, vinegar, garlic, salt",
    "peanut butter": "peanuts, salt",
    "dark chocolate": "cocoa mass, sugar, cocoa butter, milk fat, soy lecithin",
    "egg noodles": "wheat flour, eggs",
    "egg roll wrappers": "wheat flour, water, eggs, salt",
    "tortilla chips": "corn, vegetable oil, salt",
    "corn tortillas": "corn masa, water, lime",
    "rice noodles": "rice flour, water",
    "barbecue sauce": "tomato paste, vinegar, sugar, molasses, salt",
    "marinara sauce": "tomatoes, olive oil, garlic, basil, salt",
    "nori": "seaweed",
    "croutons": "wheat flour, soybean oil, salt, garlic",
    "half-and-half": "milk, cream",
    "imitation crab": "pollock, water, wheat starch, sugar, egg whites, crab extract, salt",
}
WHEAT_WORDS = re.compile(r"\b(?:flour|bread|breadcrumbs|crumbs|crust|spaghetti|linguine|"
                         r"macaroni|rotini|ziti|pasta|noodles|couscous|tortillas|buns|"
                         r"rolls|biscuits|crackers|naan|pita|dough|wrappers)\b")
NOT_WHEAT = re.compile(r"\b(?:rice|corn|cornmeal|almond|coconut|chickpea|gluten-free)\b")
SAFE_BASE = "water, coconut oil, potato starch, pea protein, sea salt"
# Foods that are the allergen itself: no store sells a "peanut-free peanut", so these get no
# free-from version (the list then offers a substitute or leaves them out)
NO_FREE_FROM = re.compile(
    r"\b(?:peanuts?|almonds?|cashews?|walnuts?|pecans?|pistachios?|hazelnuts?|macadamias?|"
    r"pine nuts|sesame|tahini|tofu|tempeh|edamame|soybeans?|miso|eggs?|egg whites|egg yolks|"
    r"salmon|tuna|cod|anchov(?:y|ies)|tilapia|fish|shrimp|crab|lobster|scallops?|clams?|"
    r"mussels?|oysters?)\b")
FREE_FROM = {"milk": "Dairy-Free", "egg": "Egg-Free", "fish": "Plant-Based",
             "shellfish": "Plant-Based", "tree_nuts": "Nut-Free", "peanuts": "Peanut-Free",
             "wheat": "Gluten-Free", "gluten": "Gluten-Free", "soy": "Soy-Free",
             "sesame": "Sesame-Free", "vegetarian": "Plant-Based", "vegan": "Vegan"}
BRANDS = ["Harvest Lane", "Brightfield", "Old Mill", "Market Basics", "Sunny Acres"]
ALT_BRAND = "Greenway"
SIZES = {"produce": "1 lb", "spice_herb": "2 oz", "dairy": "16 oz", "egg": "12 ct",
         "meat": "1 lb", "seafood": "12 oz", "grain": "16 oz", "bakery": "20 oz",
         "baking": "2 lb", "condiment": "12 oz", "sweetener": "16 oz", "oil_fat": "16 fl oz",
         "legume": "15 oz", "nut_seed": "8 oz", "beverage": "32 fl oz"}
AISLES = {"produce": "Produce", "dairy": "Dairy", "egg": "Dairy", "meat": "Meat",
          "seafood": "Seafood", "bakery": "Bakery", "grain": "Pasta & Grains",
          "baking": "Baking", "spice_herb": "Spices", "condiment": "Condiments",
          "sweetener": "Baking", "oil_fat": "Oils", "legume": "Canned Goods",
          "nut_seed": "Snacks", "beverage": "Beverages"}
STORES = [("DEMO0001", "Demo Market Downtown", "120 Main St", 0.7, 0.004, -0.006),
          ("DEMO0002", "Demo Market Riverside", "48 River Rd", 1.6, -0.012, 0.015),
          ("DEMO0003", "Demo Fresh Eastgate", "905 Eastgate Blvd", 3.4, 0.021, 0.044)]
BASE_LAT, BASE_LON = 39.1031, -84.5120


def composition(ingredient: str) -> str:
    text = COMPOSITION.get(ingredient, ingredient)
    if text == ingredient and WHEAT_WORDS.search(ingredient) and not NOT_WHEAT.search(ingredient):
        text = f"{ingredient}, wheat flour"
    return text


def _title(name: str) -> str:
    """'half-and-half' -> 'Half-and-Half', 'all-purpose flour' -> 'All-Purpose Flour'."""
    small = {"and", "or", "of", "with", "in"}
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0) if m.group(0) in small and m.start()
                  else m.group(0).capitalize(), name)


def _conflicts(evidence: ProductEvidence, avoid: list[str], diet: str) -> list[str]:
    """Which of the shopper's restrictions this product breaks on its own."""
    found = [a for a in avoid if assess(evidence, [a], "none").verdict.value == "unsafe"]
    if diet != "none" and assess(evidence, [], diet).verdict.value == "unsafe":
        found.append(diet)
    return found


def synthetic_result(dish: dict[str, Any], request: dict[str, Any]) -> Result:
    """Stores and products for one request: the same every time for the same items and ZIP."""
    seed = zlib.crc32(json.dumps([dish["name"], request], sort_keys=True).encode())
    rng = random.Random(seed)
    avoid, diet = request["avoid"], request["diet"]
    stores = [Store(store_id=sid, chain="DEMO", name=name,
                    address=f"{street}, Demo City {request['zip']}",
                    lat=round(BASE_LAT + dlat, 4), lon=round(BASE_LON + dlon, 4),
                    distance_miles=miles)
              for sid, name, street, miles, dlat, dlon in STORES]
    def product(evidence: ProductEvidence | None, name: str, brand: str, store: Store,
                category: str) -> Product:
        verdict = assess(evidence, avoid, diet)   # the real diet rules
        return Product(store_id=store.store_id, barcode=None, name=name, brand=brand,
                       size=SIZES.get(category, "1 ct"), aisle=AISLES.get(category),
                       in_stock=rng.choices([True, None, False], weights=[85, 10, 5])[0],
                       verdict=verdict.verdict, reason=verdict.reason,
                       evidence_source=verdict.evidence_source)

    ingredients = []
    for ing in dish["ingredients"]:
        name, category = ing["name"], ing["category"]
        # the same item near the same ZIP gets the same products, whatever the dish (as a
        # real store would): a build's picks are then found again by the final search
        rng.seed(zlib.crc32(json.dumps([name, request["zip"]]).encode()))
        products: list[Product] = []
        regular = None
        for store in stores:
            brand = rng.choice(BRANDS)
            regular = ProductEvidence(source=rng.choice(["kroger", "off"]),
                                      name=f"{brand} {_title(name)}", brand=brand,
                                      categories=[category], ingredients_text=composition(name))
            products.append(product(regular, regular.name, brand, store, category))
        breaks = _conflicts(regular, avoid, diet) if regular else []
        if breaks and not NO_FREE_FROM.search(name) and rng.random() < 0.65:
            # a free-from version, at one store
            labels = list(dict.fromkeys(FREE_FROM[b] for b in breaks))
            alt_name = f"{ALT_BRAND} {' '.join(labels)} {_title(name)}"
            alt = ProductEvidence(source="off", name=alt_name, brand=ALT_BRAND,
                                  categories=[category], ingredients_text=SAFE_BASE)
            products.append(product(alt, alt_name, ALT_BRAND, rng.choice(stores), category))
        if rng.random() < 0.4:   # a product with no ingredient list: never shown as safe
            brand = rng.choice(BRANDS)
            products.append(product(None, f"{brand} {_title(name)} (store brand)", brand,
                                     rng.choice(stores), category))
        ingredients.append(IngredientResult(name=name, role=ing["role"], category=category,
                                            products=products))
    now = datetime.now(UTC)
    return Result(
        request=request,
        dish=DishInfo(name=dish["name"], cuisine=dish["cuisine"], source="catalog"),
        stores=stores, ingredients=ingredients,
        summary=Summary.from_ingredients(ingredients),
        meta=Meta(generated_at=now, expires_at=now + timedelta(hours=24),
                  providers_used=["demo"], catalog_version="demo"),
    )


# ---------------------------------------------------------------- the answer

DIET_EXCLUDES = {"none": set(), "vegetarian": {"meat", "fish", "shellfish"},
                 "vegan": {"meat", "fish", "shellfish", "animal", "milk", "egg"}}
DEFAULT_WARNINGS = {
    "unknown": "Some products had no full ingredient list to check, so they are marked "
               "couldn't verify: treat them as not safe until you read the label.",
    "substitutes": "Substitutes are suggestions, not checked products: pick one marked safe "
                   "or read its label.",
    "unsupported": "We can't check {label} in this version: please confirm it yourself.",
    "clarify": "You mentioned nuts: we avoided tree nuts. Tell us if peanuts should be "
               "avoided too.",
    "off_attribution": "Product data: Open Food Facts (openfoodfacts.org)",
}
DEMO_WARNING = ("Demo mode: these stores and products are synthetic. Don't shop from this "
                "list.")


class SubstituteTable:
    """ml/configs/substitutes.yaml, matched the way A3 matches it: the key that ends the
    ingredient's name, longest first, plurals too; the first candidate that fits wins."""

    def __init__(self, path: Path = SUBSTITUTES_FILE):
        data = yaml.safe_load(path.read_text()) if path.is_file() else {}
        self.table: dict[str, list[dict[str, Any]]] = (data or {}).get("substitutes") or {}
        self.keys = sorted(self.table, key=len, reverse=True)

    def pick(self, ingredient: str, avoid: list[str], diet: str) -> str | None:
        name = ingredient.lower()
        for key in self.keys:
            if any(name == form or name.endswith(" " + form)
                   for form in (key, key + "s", key + "es")):
                blocked = set(avoid) | DIET_EXCLUDES[diet]
                return next((c["name"] for c in self.table[key]
                             if not blocked & set(c.get("contains") or [])
                             and c["name"].lower() != name), None)
        return None


def decide(result: dict[str, Any], table: SubstituteTable) -> list[dict[str, Any]]:
    """Each ingredient's status: buy (nearest safe product), check_label, substitute or
    skip. The same order of preference as A3's answer.decide."""
    distance = {s["store_id"]: s["distance_miles"] for s in result["stores"]}
    avoid, diet = result["request"]["avoid"], result["request"]["diet"]
    restrictions = join_words([allergen_label(a) for a in avoid])
    items = []
    for ing in result["ingredients"]:
        item = {"name": ing["name"], "status": "skip", "product": None, "store_id": None,
                "substitute": None, "quantity": None, "unit": None, "note": None}
        verdicts = [p["verdict"] for p in ing["products"]]
        safe = [p for p in ing["products"] if p["verdict"] == "safe"]
        if safe:
            best = min(safe, key=lambda p: distance.get(p["store_id"], 99.0))
            item.update(status="buy", product=best["name"], store_id=best["store_id"])
            items.append(item)
            continue
        inherent = "unsafe" in verdicts
        substitute = table.pick(ing["name"], avoid, diet)
        if "unknown" in verdicts and not inherent:
            item.update(status="check_label",
                        note="Some products couldn't be verified: read the label"
                             + (f" for {restrictions}" if restrictions else "") + ".")
        elif substitute:
            item.update(status="substitute", substitute=substitute,
                        note=f"No product was marked safe: use {substitute}.")
        elif ing["role"] == "common":
            item.update(note="No safe product: the dish works without it.")
        else:
            needs = restrictions or "the avoided allergens"
            if diet != "none":
                needs += f" that fits a {diet} diet"
            item.update(status="substitute",
                        note=f"No safe product found: choose a substitute without {needs}.")
        items.append(item)
    return items


def summary_text(result: dict[str, Any], items: list[dict[str, Any]],
                 servings: int | None) -> str:
    request, n = result["request"], len(items)
    count = {s: sum(1 for i in items if i["status"] == s)
             for s in ("buy", "substitute", "check_label", "skip")}
    stores = {i["store_id"] for i in items if i["store_id"]}
    who = []
    if request["diet"] != "none":
        who.append(request["diet"])
    if request["avoid"]:
        who.append("without " + join_words([allergen_label(a) for a in request["avoid"]]))
    text = f"{result['dish']['name'].capitalize()}"
    text += f" for {servings}" if servings else ""
    text += f", {', '.join(who)}" if who else ""
    text += (f". Safe products for {count['buy']} of {n} ingredients at "
             f"{len(stores)} nearby store{'s' if len(stores) != 1 else ''}.")
    extra = []
    if count["substitute"]:
        extra.append(f"{count['substitute']} need{'s' * (count['substitute'] == 1)} a substitute")
    if count["check_label"]:
        extra.append(f"{count['check_label']} need{'s' * (count['check_label'] == 1)} a label "
                     "check")
    if count["skip"]:
        extra.append(f"{count['skip']} can be left out")
    if extra:
        text += " " + join_words(extra).capitalize() + "."
    return text


def demo_warnings(result: dict[str, Any], items: list[dict[str, Any]],
                  extras: dict[str, Any], clarify: list[str]) -> list[str]:
    texts = DEFAULT_WARNINGS
    if ANSWER_CONFIG_FILE.is_file():
        texts = {**texts, **(yaml.safe_load(ANSWER_CONFIG_FILE.read_text()).get("warnings")
                             or {})}
    out = [DEMO_WARNING]
    if any(p["verdict"] == "unknown" for i in result["ingredients"] for p in i["products"]):
        out.append(texts["unknown"])
    if any(i["status"] == "substitute" for i in items):
        out.append(texts["substitutes"])
    for need in extras.get("unsupported") or []:
        out.append(texts["unsupported"].format(label=need.replace("_", " ")))
    if "peanuts" in clarify:
        out.append(texts["clarify"])
    out.append(texts["off_attribution"])
    return out


# ---------------------------------------------------------------- the engine


class DemoEngine:
    mode = "demo"

    def __init__(self, delay_s: float = 1.2):
        self.catalog = DemoCatalog()
        self.table = SubstituteTable()
        self.delay_s = delay_s
        self._screen: Any = None

    def info(self) -> dict[str, Any]:
        return {"mode": self.mode, "dishes": len(self.catalog.dishes),
                "examples": ["quiche lorraine", "pad thai", "chicken tikka masala",
                             "banana bread", "vegan pancakes"],
                "a3_available": True,
                # the demo's dishes all come from its catalog: nothing for a stronger
                # model to redo
                "stronger_model": {"available": False, "label": None}}

    def parse(self, messages: list[dict[str, str]]) -> ParseOutcome:
        return read_chat(messages, self.catalog)

    def search(self, request: dict[str, Any], extras: dict[str, Any], clarify: list[str],
               progress: Progress, *, stronger: bool = False) -> SearchOutcome:
        if stronger:
            raise EngineError("The demo has no stronger model.", kind="stronger_unavailable")
        dish = (self.catalog.by_name.get(request["query"]) or self.catalog.match(request["query"])
                or _template_as_dish(request["query"]))
        if dish is None:
            raise EngineError(
                f"The demo doesn't know a dish called \"{request['query']}\". It knows "
                f"{len(self.catalog.dishes)} dishes, for example quiche lorraine, pad thai "
                "or banana bread.", kind="not_found")
        progress("searching")
        time.sleep(self.delay_s)
        result = synthetic_result(dish, request).model_dump(mode="json")
        return self._answer(result, extras, clarify, progress)

    # ------------------------------------------------------------ guided builds

    def plan(self, query: str, avoid: list[str], diet: str) -> Any:
        """A template, else one of the demo's dishes; the demo has no model for others."""
        from app.plans import PlanError, build_plan  # plans -> screen -> this module

        def catalog(text: str) -> tuple[str, str, list[dict[str, Any]]] | None:
            dish = self.catalog.by_name.get(text) or self.catalog.match(text)
            return (dish["name"], dish["cuisine"], dish["ingredients"]) if dish else None

        try:
            return build_plan(query, avoid, diet, self.screen, catalog=catalog)
        except PlanError as exc:
            raise EngineError(
                f"The demo doesn't know \"{query}\". Try a kind of food (sandwich, salad, "
                "pasta, wrap, bowl) or one of its dishes, for example quiche lorraine.",
                kind="not_found") from exc

    @property
    def screen(self) -> Any:
        if self._screen is None:
            from app.screen import Screen
            self._screen = Screen(self.table)
        return self._screen

    def prefetch(self, zip_code: str, terms: list[str]) -> None:
        """Synthetic stores need no fetching."""

    def options(self, request: dict[str, Any], terms: list[str]) -> dict[str, Any]:
        dish = {"name": request["query"], "cuisine": "american",
                "ingredients": [{"name": t, "role": "core", "category": "other"}
                                for t in dict.fromkeys(terms)]}
        return synthetic_result(dish, request).model_dump(mode="json")

    def search_ingredients(self, request: dict[str, Any], guided: dict[str, Any],
                           extras: dict[str, Any], clarify: list[str],
                           progress: Progress, *, answer: bool = True) -> SearchOutcome:
        progress("searching")
        time.sleep(self.delay_s)
        dish = {"name": guided["dish"], "cuisine": guided.get("cuisine") or "american",
                "ingredients": [{**i, "name": i.get("term") or i["name"]}
                                for i in guided["ingredients"]]}
        result = synthetic_result(dish, request).model_dump(mode="json")
        for found, item in zip(result["ingredients"], guided["ingredients"], strict=True):
            found["name"] = item["name"]   # searched by term, reported by item name
        if not answer:
            return SearchOutcome(result=result, answer_status="skipped",
                                 warnings=[DEMO_WARNING])
        chosen = guided.get("choices") or {}
        if not chosen:
            return self._answer(result, extras, clarify, progress)
        from app.choices import narrow
        outcome = self._answer(narrow(result, chosen), extras, clarify, progress)
        outcome.result, outcome.choices = result, chosen   # every product, and the picks
        return outcome

    def _answer(self, result: dict[str, Any], extras: dict[str, Any], clarify: list[str],
                progress: Progress) -> SearchOutcome:
        progress("answering")
        time.sleep(self.delay_s * 0.6)
        items = decide(result, self.table)
        servings = extras.get("servings")
        answer = {"summary": summary_text(result, items, servings), "servings": servings,
                  "ingredients": items, "steps": []}
        return SearchOutcome(result=result, answer=answer, answer_status="ok",
                             warnings=demo_warnings(result, items, extras, clarify))
