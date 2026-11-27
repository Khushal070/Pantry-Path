"""Product choices for a guided build: the main store, each option's pick and alternatives,
favourite brands and family hints. Plain functions over result.json-shaped data
({stores, ingredients: [{name, products}]}), which lives in memory only (Kroger data).

    main store   the store with a safe product for the most usual options, nearest on a tie
    pick         the favourite brand if found and not unsafe; else the first safe product at
                 the main store; else the nearest other store's safe product; else one to
                 read the label of (unknown). Unsafe products are never picked or offered.
    hint         a favourite brand of the same family ("cheese"), offered first, not picked
"""

import re
from typing import Any

MAX_ALTERNATIVES = 3
VERDICT_RANK = {"safe": 0, "unknown": 1}


def family(item: str) -> str:
    """'swiss cheese' -> 'cheese', 'sourdough bread' -> 'bread', 'tortillas' -> 'tortilla'."""
    word = (item.strip().lower().split() or [""])[-1]
    return re.sub(r"(?<=[^su])s$", "", word)   # not hummus, asparagus, couscous


def brand_term(brand: str, item: str) -> str:
    """What to search for a favourite: 'Dave's Killer Bread sandwich bread'."""
    return f"{brand} {item}"


def search_terms(items: list[str], favourites: dict[str, str]) -> dict[str, dict[str, str]]:
    """Per item, the extra searches for favourites: {item: {"favourite": term} and/or
    {"hint": term, "hint_brand": brand}}."""
    by_family: dict[str, str] = {}
    for fav_item, brand in favourites.items():
        by_family.setdefault(family(fav_item), brand)
    extra: dict[str, dict[str, str]] = {}
    for item in items:
        key = item.lower()
        if key in favourites:
            extra[item] = {"favourite": brand_term(favourites[key], item),
                           "brand": favourites[key]}
        elif family(item) in by_family:
            brand = by_family[family(item)]
            extra[item] = {"hint": brand_term(brand, item), "hint_brand": brand}
    return extra


def product_key(product: dict[str, Any]) -> str:
    return f"{product['store_id']}|{product.get('barcode') or product['name']}"


def _view(product: dict[str, Any], store: dict[str, Any] | None, main: str | None,
          term: str) -> dict[str, Any]:
    """term: what was searched to find it (the item, or "brand item" for a favourite or
    hint), so the final search can look it up the same way."""
    return {"key": product_key(product), "name": product["name"], "term": term,
            "brand": product.get("brand") or "", "size": product.get("size") or "",
            "aisle": product.get("aisle"), "verdict": product["verdict"],
            "reason": product.get("reason") or "", "store_id": product["store_id"],
            "store": (store or {}).get("name") or "", "miles": (store or {}).get("distance_miles"),
            "other_store": main is not None and product["store_id"] != main}


def products_of(found: dict[str, Any], term: str) -> list[dict[str, Any]]:
    ingredient = next((i for i in found.get("ingredients", []) if i["name"] == term), None)
    return list(ingredient["products"]) if ingredient else []


def main_store(found: dict[str, Any], usual: list[str]) -> str | None:
    stores = sorted(found.get("stores", []), key=lambda s: s.get("distance_miles") or 0)
    if not stores:
        return None
    terms = usual or [i["name"] for i in found.get("ingredients", [])]

    def covered(store_id: str) -> int:
        return sum(any(p["store_id"] == store_id and p["verdict"] == "safe"
                       for p in products_of(found, t)) for t in terms)

    return max(stores, key=lambda s: (covered(s["store_id"]), -(s.get("distance_miles") or 0))
               )["store_id"]


def _ranked(products: list[dict[str, Any]], main: str | None,
            miles: dict[str, float]) -> list[dict[str, Any]]:
    """Not unsafe; safe before unknown; the main store before others; nearer first; then
    the store's own relevance order."""
    usable = [(i, p) for i, p in enumerate(products) if p["verdict"] in VERDICT_RANK]
    usable.sort(key=lambda ip: (VERDICT_RANK[ip[1]["verdict"]], ip[1]["store_id"] != main,
                                miles.get(ip[1]["store_id"], 99), ip[0]))
    return [p for _, p in usable]


def _brand_match(product: dict[str, Any], brand: str) -> bool:
    """The product is of this brand: its brand or name contains it, or (a short store
    brand like "Kroger") its brand is part of it."""
    wanted = brand.lower().replace("’", "'")
    own = (product.get("brand") or "").lower().replace("’", "'")
    text = f"{own} {product['name'].lower()}".replace("’", "'")
    return wanted in text or (len(own) >= 3 and own in wanted)


def choice_for(item: str, found: dict[str, Any], main: str | None,
               extra: dict[str, str] | None = None) -> dict[str, Any]:
    """One option's pick, alternatives and hint."""
    stores = {s["store_id"]: s for s in found.get("stores", [])}
    miles = {sid: s.get("distance_miles") or 0 for sid, s in stores.items()}
    products = products_of(found, item)
    ranked = _ranked(products, main, miles)
    hidden = sum(p["verdict"] == "unsafe" for p in products)
    extra = extra or {}
    favourite = extra.get("brand")
    pick = None
    favourite_missing = False
    if favourite:
        fav = [p for p in _ranked(products_of(found, extra["favourite"]), main, miles)
               if _brand_match(p, favourite)]
        if fav:
            pick = fav[0]
        else:
            favourite_missing = True
    if pick is None and ranked:
        pick = ranked[0]
    hint = None
    if extra.get("hint_brand"):
        found_hint = [p for p in _ranked(products_of(found, extra["hint"]), main, miles)
                      if _brand_match(p, extra["hint_brand"])]
        hint = found_hint[0] if found_hint else None
    others = [p for p in ranked if pick is None or product_key(p) != product_key(pick)]
    if hint is not None:
        others = [p for p in others if product_key(p) != product_key(hint)]
    pick_term = extra["favourite"] if favourite and not favourite_missing else item
    return {
        "pick": _view(pick, stores.get(pick["store_id"]), main, pick_term) if pick else None,
        "alternatives": [_view(p, stores.get(p["store_id"]), main, item)
                         for p in others[:MAX_ALTERNATIVES]],
        "hint": _view(hint, stores.get(hint["store_id"]), main, extra["hint"])
        if hint else None,
        "hint_brand": extra.get("hint_brand"),
        "favourite": favourite, "favourite_missing": favourite_missing,
        "hidden_unsafe": hidden,
    }


def find_view(choice: dict[str, Any], key: str) -> dict[str, Any] | None:
    """The product with this key among a choice's pick, hint and alternatives."""
    for view in [choice.get("pick"), choice.get("hint"), *choice.get("alternatives", [])]:
        if view and view["key"] == key:
            return view
    return None


def narrow(result: dict[str, Any], chosen: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """result.json with each chosen item's products narrowed to the chosen product, so A3
    (and the demo's list) buys exactly that one. Items without a choice, or whose chosen
    product isn't in this result, keep all their products."""
    out = {**result, "ingredients": []}
    for ingredient in result.get("ingredients", []):
        pick = chosen.get(ingredient["name"])
        kept = [p for p in ingredient["products"] if pick and product_key(p) == pick["key"]]
        out["ingredients"].append({**ingredient, "products": kept or ingredient["products"]})
    return out
