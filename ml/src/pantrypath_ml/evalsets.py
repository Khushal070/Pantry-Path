"""Evaluation sets for A2 (unseen dishes) and A1 (request parsing).

A2 splits:
- dev: data/golden/recipes.json, looked at while building the pipeline (optimistic);
- test: data/golden/recipes_heldout.json, held out: read only when asked for explicitly;
- loo (leave-one-out): every catalog dish asked by name only, its catalog entry as the
  reference, without the golden test dishes (search and selection read it). Catalog
  entries are LLM-written, so this is a consistency check with enough dishes to slice by
  cuisine and popularity, not ground truth. The reference's core ingredients count as
  required; common ones as optional.
- loo_full: the same with every catalog dish, the golden test dishes too: held out, for
  the A2 bias check in `select final` only.
Golden recipes are read with the pipeline's loader and matched to catalog dishes with the
same dish_ids overrides as `evaluate.run_catalog`, which gives them cuisine and popularity.

A1: generated from catalog dish names and aliases with the phrase bank in
configs/evalsets.yaml (seeded), split by dish so test dishes never appear in tuning:
    python -m pantrypath_ml.evalsets generate-a1
A3: one synthetic result.json per catalog dish (synthetic stores and products, never
Kroger data), with the same dev/test dishes as A1, validated against the contract:
    python -m pantrypath_ml.evalsets generate-a3
The generated chats share their wording with the cross-check's word lists, so A1 also has
a hand-written `realistic` split (evalsets/a1_realistic.jsonl): everyday phrasing the
templates don't produce. It is held out like `test` and read only by `select final`.
"""

import argparse
import csv
import json
import logging
import random
import re
from pathlib import Path
from typing import Any

from pantrypath_core.catalog import slugify
from pantrypath_core.diet_rules import Allergen
from pantrypath_ml import config
from pantrypath_ml.reporting import read_jsonl, write_jsonl
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import DataUnavailableError, PipelineError
from pantrypath_pipeline.evaluate import find_dish, read_recipes
from pantrypath_pipeline.logging_setup import setup_logging

log = logging.getLogger(__name__)

A2_SPLITS = ("dev", "loo", "loo_full", "test")
A1_SPLITS = ("dev", "test", "realistic", "realistic_dev")
HELD_OUT = ("test", "realistic", "loo_full")  # read only for gates and final results
ALLERGEN_ORDER = [a.value for a in Allergen]
DIET_ORDER = ["none", "vegetarian", "vegan"]  # least to most strict


class HeldOutError(PipelineError):
    """The test split was requested without saying so explicitly."""


def _guard_test(split: str, allow_test: bool) -> None:
    if split in HELD_OUT:
        if not allow_test:
            raise HeldOutError(f"The {split} split is held out: read it only for gates and "
                               f"final results, by asking for it explicitly (--split {split})")
        log.warning("Reading the held-out %s split (gates and final results only)",
                    split.upper())


# ---------------------------------------------------------------- catalog


def load_catalog(pcfg: Config) -> list[dict[str, Any]]:
    path = pcfg.path(pcfg["catalog"]["profiles_output"])
    if not path.is_file():
        raise DataUnavailableError(
            f"Dish catalog {path} not found; run `dvc pull "
            "data-pipeline/data/processed/dish_profiles.json.dvc` from the repo root")
    return json.loads(path.read_text())["dishes"]


def popularity_tiers(catalog: list[dict[str, Any]], labels: list[str]) -> dict[str, str]:
    """dish_id -> tier, most-reviewed dishes first, tiers as equal as possible."""
    ranked = sorted(catalog, key=lambda d: (-int(d.get("review_count") or 0),
                                            -int(d.get("recipe_count") or 0), d["dish_id"]))
    n = len(ranked)
    return {d["dish_id"]: labels[min(i * len(labels) // n, len(labels) - 1)]
            for i, d in enumerate(ranked)}


# ---------------------------------------------------------------- A2


def a2_cases(pcfg: Config, ecfg: Config, split: str, *,
             allow_test: bool = False) -> list[dict[str, Any]]:
    """A2 cases: {case_id, name (asked), reference (recipe), dish_id, cuisine, popularity}."""
    if split not in A2_SPLITS:
        raise PipelineError(f"Unknown A2 split {split!r}; use one of {A2_SPLITS}")
    _guard_test(split, allow_test)
    catalog = load_catalog(pcfg)
    tiers = popularity_tiers(catalog, ecfg["a2"]["popularity_tiers"])
    if split in ("loo", "loo_full"):
        held_out = golden_test_dish_ids(pcfg, catalog) if split == "loo" else set()
        log.info("Leave-one-out: %d catalog dishes, %d left out (golden test dishes)",
                 len(catalog) - len(held_out), len(held_out))
        return [{
            "case_id": d["dish_id"], "name": d["name"], "dish_id": d["dish_id"],
            "cuisine": d["cuisine"], "popularity": tiers[d["dish_id"]],
            "reference": {"dish": d["name"], "ingredients": [
                {"name": i["name"], "optional": i["role"] != "core"} for i in d["ingredients"]]},
        } for d in catalog if d["dish_id"] not in held_out]

    settings = pcfg["catalog_eval"]
    overrides = {k.lower(): v for k, v in (settings.get("dish_ids") or {}).items()}
    cases = []
    for recipe in read_recipes(pcfg.path(settings["splits"][split])):
        dish = find_dish(recipe["dish"], catalog, overrides)
        cases.append({
            "case_id": slugify(recipe["dish"]), "name": recipe["dish"],
            "dish_id": dish["dish_id"] if dish else None,
            "cuisine": dish["cuisine"] if dish else None,
            "popularity": tiers[dish["dish_id"]] if dish else None,
            "reference": recipe,
        })
    return cases


def golden_test_dish_ids(pcfg: Config, catalog: list[dict[str, Any]]) -> set[str]:
    """Catalog dish_ids of the golden test recipes (same mapping as the test split), so
    the leave-one-out set, which selection reads, never asks a test dish. Only the dish
    names are used: nothing from the test recipes is scored or logged here."""
    settings = pcfg["catalog_eval"]
    overrides = {k.lower(): v for k, v in (settings.get("dish_ids") or {}).items()}
    out = set()
    for recipe in read_recipes(pcfg.path(settings["splits"]["test"])):
        dish = find_dish(recipe["dish"], catalog, overrides)
        if dish:
            out.add(dish["dish_id"])
    return out


# ---------------------------------------------------------------- A1 generator


def _join(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def typo(text: str, rng: random.Random) -> str:
    """One dropped or swapped letter in the longest word (never the first letter)."""
    words = text.split()
    i = max(range(len(words)), key=lambda k: len(words[k]))
    word = words[i]
    if len(word) < 4:
        return text
    pos = rng.randrange(1, len(word) - 1)
    if rng.random() < 0.5:
        changed = word[:pos] + word[pos + 1:]
    else:
        changed = word[:pos] + word[pos + 1] + word[pos] + word[pos + 2:]
    if changed == word:
        changed = word[:pos] + word[pos + 1:]
    words[i] = changed
    return " ".join(words)


class _Generator:
    def __init__(self, gcfg: dict[str, Any], seed: int):
        self.g = gcfg
        self.rng = random.Random(seed)

    def pick_allergens(self) -> list[str]:
        k = self.rng.choices(range(3), weights=self.g["allergen_count_weights"])[0]
        chosen = set(self.rng.sample(ALLERGEN_ORDER, k))
        return [a for a in ALLERGEN_ORDER if a in chosen]

    def words(self, allergens: list[str], synonyms: bool) -> tuple[list[str], list[str]]:
        """(words for a list, whole sentences) naming the allergens."""
        words, sentences = [], []
        for a in allergens:
            options = self.g["allergen_synonyms"].get(a) or []
            if synonyms and not options and self.g["allergen_sentences"].get(a):
                sentences.append(self.rng.choice(self.g["allergen_sentences"][a]))
            elif synonyms and options:
                words.append(self.rng.choice(options))
            else:
                words.append(self.g["allergen_words"][a])
        return words, sentences

    def avoid_text(self, words: list[str], sentences: list[str], formal: bool) -> str:
        parts = []
        if words:
            if formal:
                template = self.rng.choice(["I am allergic to {}.",
                                            "Please leave out anything with {}."])
            elif len(words) == 1:
                template = self.rng.choice(["No {} please.", "I can't have {}.",
                                            "{}-free please."])
            else:
                template = self.rng.choice(["No {} please.", "I can't have {}."])
            parts.append(template.format(_join(words)))
        parts += sentences
        return " ".join(parts)

    def zip_text(self) -> tuple[str, str]:
        zip5 = f"{self.rng.randrange(100000):05d}"
        fmt = self.rng.choice(self.g["zip_formats"])
        return zip5, fmt.format(zip=zip5, plus4=f"{self.rng.randrange(10000):04d}")

    def case(self, dish: dict[str, Any], style: str, case_id: str) -> dict[str, Any]:
        rng, g = self.rng, self.g
        skip = set(g["alias_skip_words"])
        aliases = [a for a in dish.get("aliases") or []
                   if not skip & set(re.findall(r"[a-z]+", a.lower()))]
        dish_text = rng.choice(aliases) if aliases and rng.random() < g["p_alias"] \
            else dish["name"]
        if style == "typo":
            dish_text = typo(dish_text, rng)
        avoid = self.pick_allergens()
        diet = rng.choices(list(g["diet_weights"]), weights=list(g["diet_weights"].values()))[0]
        # A dish named for a diet ("vegan pancakes") asks for at least that diet
        named = [d for d in DIET_ORDER if d in dish_text.lower().split()]
        expected_diet = max([diet, *named], key=DIET_ORDER.index)
        zip5, zip_text = self.zip_text()
        servings = rng.randint(2, 8) if rng.random() < g["p_servings"] else None
        unsupported = rng.choice(sorted(g["unsupported"])) \
            if rng.random() < g["p_unsupported"] else None
        formal = style in ("formal", "late")
        for_whom = f" for {servings} people" if servings else ""

        if formal:
            opening = f"I would like to prepare {dish_text}{for_whom}. I live in {zip_text}."
        else:
            opening = f"hey, can you find me stuff for {dish_text}{for_whom} near {zip_text}?"
        diet_text = rng.choice(g["diet_sentences"][diet]) if diet != "none" else ""
        extra = rng.choice(g["unsupported"][unsupported]) if unsupported else ""

        messages: list[dict[str, str]]
        if style == "multi_turn":
            first, second = self.correction(avoid)
            messages = [
                {"role": "user", "content": " ".join(p for p in (opening, first, diet_text) if p)},
                {"role": "assistant", "content": g["assistant_turn"]},
                {"role": "user", "content": " ".join(p for p in (second, extra) if p)},
            ]
        else:
            words, sentences = self.words(avoid, synonyms=style == "synonym")
            allergy = self.avoid_text(words, sentences, formal)
            if style == "negation":
                allergy = self.negation(avoid, allergy)
            if style == "late":
                body = [opening, "I want to cook it this weekend.", diet_text, extra,
                        f"Oh, and one more thing: {allergy}" if allergy else ""]
            else:
                body = [opening, allergy, diet_text, extra]
            messages = [{"role": "user", "content": " ".join(p for p in body if p)}]

        return {
            "case_id": case_id, "dish_id": dish["dish_id"], "dish_text": dish_text,
            "messages": messages,
            "expected": {"zip": zip5, "avoid": avoid, "diet": expected_diet, "servings": servings,
                         "unsupported": [unsupported] if unsupported else []},
            "slices": {"style": style, "allergens": avoid, "diet": expected_diet,
                       "has_unsupported": bool(unsupported)},
        }

    def negation(self, avoid: list[str], allergy: str) -> str:
        """Add an allergen that is explicitly fine; it must not end up in avoid."""
        fine = self.rng.choice([a for a in ALLERGEN_ORDER if a not in avoid])
        word = self.g["allergen_words"][fine]
        verb = "are" if word.endswith("s") else "is"
        fine_text = self.rng.choice([f"{word.capitalize()} {verb} fine though.",
                                     f"I'm fine with {word}.", f"I can eat {word}."])
        return " ".join(p for p in (allergy, fine_text) if p)

    def correction(self, avoid: list[str]) -> tuple[str, str]:
        """Turn 1 and turn 2 text: withdraw an extra allergen, or add the last one late."""
        words = self.g["allergen_words"]
        if avoid and self.rng.random() < 0.5:
            first = self.avoid_text([words[a] for a in avoid[:-1]], [], formal=False)
            return first, f"Oh, and no {words[avoid[-1]]} either."
        extra = self.rng.choice([a for a in ALLERGEN_ORDER if a not in avoid])
        listed = [a for a in ALLERGEN_ORDER if a in {*avoid, extra}]
        first = self.avoid_text([words[a] for a in listed], [], formal=False)
        verb = "are" if words[extra].endswith("s") else "is"
        return first, f"Sorry, actually {words[extra]} {verb} fine."


def held_out_dish_ids(catalog: list[dict[str, Any]], seed: int, fraction: float) -> set[str]:
    """The catalog dishes held out for testing (A1 and A3 use the same ones)."""
    ids = sorted(d["dish_id"] for d in catalog)
    random.Random(seed).shuffle(ids)
    return set(ids[:round(len(ids) * float(fraction))])


def generate_a1(catalog: list[dict[str, Any]], gcfg: dict[str, Any]) -> dict[str, list[dict]]:
    """{'dev': [...], 'test': [...]}, split by dish, the same for the same seed and catalog."""
    dishes = sorted(catalog, key=lambda d: d["dish_id"])
    test_ids = held_out_dish_ids(catalog, gcfg["seed"], gcfg["test_fraction"])
    gen = _Generator(gcfg, gcfg["seed"])
    styles = gcfg["styles"]
    splits: dict[str, list[dict]] = {"dev": [], "test": []}
    for n, dish in enumerate(dishes):
        split = "test" if dish["dish_id"] in test_ids else "dev"
        for k in range(int(gcfg["cases_per_dish"])):
            style = styles[(n * int(gcfg["cases_per_dish"]) + k) % len(styles)]
            case = gen.case(dish, style, f"a1-{dish['dish_id']}-{k}")
            splits[split].append({**case, "split": split})
    return splits


def review_rows(splits: dict[str, list[dict]], size: int, seed: int) -> list[dict[str, Any]]:
    cases = sorted((c for s in splits.values() for c in s), key=lambda c: c["case_id"])
    sample = sorted(random.Random(seed).sample(cases, min(size, len(cases))),
                    key=lambda c: c["case_id"])
    return [{
        "case_id": c["case_id"], "split": c["split"], "style": c["slices"]["style"],
        "chat": " | ".join(f"{m['role']}: {m['content']}" for m in c["messages"]),
        "expected_avoid": " ".join(c["expected"]["avoid"]), "expected_diet": c["expected"]["diet"],
        "expected_zip": c["expected"]["zip"], "expected_servings": c["expected"]["servings"] or "",
        "expected_unsupported": " ".join(c["expected"]["unsupported"]),
        "reviewer_ok": "", "reviewer_note": "",
    } for c in sample]


def a1_path(ecfg: Config, split: str) -> Path:
    return ecfg.path(ecfg["a1"]["output_dir"]) / f"a1_{split}.jsonl"


def write_a1(pcfg: Config, ecfg: Config) -> dict[str, Any]:
    gcfg = ecfg["a1"]
    splits = generate_a1(load_catalog(pcfg), gcfg)
    for split, cases in splits.items():
        write_jsonl(a1_path(ecfg, split), cases)
    review = ecfg.path(gcfg["output_dir"]) / "a1_review_sample.csv"
    rows = review_rows(splits, int(gcfg["review_sample"]), gcfg["seed"])
    with open(review, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {split: len(cases) for split, cases in splits.items()}
    summary["review_sample"] = str(review)
    log.info("A1 evaluation sets written: %s", summary)
    return summary


# The hand-written chats of the independent draft (evalsets/a1_realistic_dev_source.csv):
# servings the CSV has no column for, read from the chat by hand
DRAFT_SERVINGS = {"ind-02": 6, "ind-08": 2, "ind-20": 4, "ind-27": 8}
FOLLOW_UP = "Got it. Anything else I should know?"


def convert_a1_csv(pcfg: Config, ecfg: Config, csv_path: Path) -> dict[str, Any]:
    """The independent draft CSV -> evalsets/a1_realistic_dev.jsonl, a DEV split of
    everyday phrasing (tuned on; the held-out realistic split stays untouched). Each dish
    is resolved with the request flow's catalog lookup; chats whose dish doesn't resolve,
    or is an A1 test dish, are left out and listed."""
    from pantrypath_ml.parse_request import ALLERGEN_ORDER
    from pantrypath_pipeline.store import find_dish as find

    db = pcfg.path(pcfg["catalog"]["sqlite_output"])
    min_score = float(pcfg["request"]["fuzzy_min_score"])
    a1 = ecfg["a1"]
    test_ids = held_out_dish_ids(load_catalog(pcfg), a1["seed"], a1["test_fraction"])
    labels = set(config.load("parse_request")["unsupported_labels"])
    cases, unresolved, test_dish = [], [], []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            dish = find(db, row["dish"], min_score)
            if dish is None:
                unresolved.append({"id": row["id"], "dish": row["dish"]})
                continue
            if dish.dish_id in test_ids:
                test_dish.append({"id": row["id"], "dish": row["dish"], "dish_id": dish.dish_id})
                continue
            messages = [{"role": "user", "content": row["message_1"]}]
            if row["message_2"].strip():
                messages += [{"role": "assistant", "content": FOLLOW_UP},
                             {"role": "user", "content": row["message_2"]}]
            avoid = [a for a in ALLERGEN_ORDER if a in row["avoid"].split(";")]
            unsupported = [u.strip().replace(" ", "_") for u in row["unsupported"].split(";")
                           if u.strip()]
            bad = [u for u in unsupported if u not in labels]
            if bad:
                raise PipelineError(f"{row['id']}: unknown unsupported label(s) {bad}")
            cases.append({
                "case_id": f"a1rd-{row['id']}", "dish_id": dish.dish_id, "dish_text": row["dish"],
                "messages": messages,
                "expected": {"zip": row["zip"] or None, "avoid": avoid, "diet": row["diet"],
                             "servings": DRAFT_SERVINGS.get(row["id"]),
                             "unsupported": unsupported},
                "slices": {"style": "realistic", "allergens": avoid, "diet": row["diet"],
                           "has_unsupported": bool(unsupported)},
                "split": "realistic_dev"})
    write_jsonl(a1_path(ecfg, "realistic_dev"), cases)
    out = {"cases": len(cases), "unresolved": unresolved, "test_dishes_left_out": test_dish,
           "path": str(a1_path(ecfg, "realistic_dev"))}
    log.info("A1 realistic_dev: %d cases; %d unresolved, %d on test dishes left out",
             len(cases), len(unresolved), len(test_dish))
    return out


def load_a1(ecfg: Config, split: str, *, allow_test: bool = False) -> list[dict[str, Any]]:
    if split not in A1_SPLITS:
        raise PipelineError(f"Unknown A1 split {split!r}; use one of {A1_SPLITS}")
    _guard_test(split, allow_test)
    path = a1_path(ecfg, split)
    if not path.is_file():
        if split == "realistic":
            raise DataUnavailableError(f"{path} not found; it is hand-written, not generated")
        raise DataUnavailableError(f"{path} not found; run python -m pantrypath_ml.evalsets "
                                   "generate-a1")
    cases = read_jsonl(path)
    if split == "realistic":
        # removed for independence: worded like the cross-check fixes (unit tests only)
        excluded = set(ecfg["a1"].get("realistic_excluded") or [])
        cases = [c for c in cases if c["case_id"] not in excluded]
    return cases


# ---------------------------------------------------------------- A3: synthetic results

STORES = [
    {"store_id": "SYN00001", "chain": "KROGER", "name": "Synthetic Market North",
     "address": "1 Test Ave, Testville, OH 45202", "lat": 39.11, "lon": -84.51,
     "distance_miles": 0.8},
    {"store_id": "SYN00002", "chain": "KROGER", "name": "Synthetic Market South",
     "address": "2 Test Ave, Testville, OH 45202", "lat": 39.09, "lon": -84.52,
     "distance_miles": 2.1},
]
# allergens an ingredient's category suggests (with the name check below)
CATEGORY_ALLERGENS = {"dairy": ["milk"], "egg": ["egg"], "seafood": ["fish", "shellfish"],
                      "nut_seed": ["tree_nuts", "peanuts", "sesame"],
                      "grain": ["wheat", "gluten"], "bakery": ["wheat", "gluten"],
                      "legume": ["soy"]}
BRANDS = {"safe": "Brightfield", "unsafe": "Harvest Lane", "unknown": "Old Mill"}


def synthetic_result(dish: dict[str, Any], avoid: list[str], diet: str, rng: random.Random,
                     screen: Any, gcfg: dict[str, Any]) -> dict[str, Any]:
    """A result.json for a catalog dish with synthetic stores and products (never Kroger
    data). A product conflicts when the ingredient's name or category does; some products
    are unknown, as in real results."""
    unknown_share = float(gcfg["unknown_share"])
    ingredients = []
    for ing in dish["ingredients"]:
        category = ing.get("category")
        conflict = (bool(screen.problems(ing["name"], avoid, diet))
                    or any(a in avoid for a in CATEGORY_ALLERGENS.get(category, []))
                    or (diet != "none" and category in ("meat", "seafood"))
                    or (diet == "vegan" and category in ("dairy", "egg")))
        products = []
        for store in rng.sample(STORES, rng.randint(1, len(STORES))):
            verdict = "unsafe" if conflict else "safe"
            if rng.random() < unknown_share:
                verdict = "unknown"
            products.append({
                "store_id": store["store_id"], "barcode": None,
                "name": f"{BRANDS[verdict]} {ing['name'].title()} {store['store_id'][-1]}",
                "brand": BRANDS[verdict], "size": "1 ct", "in_stock": True, "aisle": None,
                "verdict": verdict,
                "reason": {"safe": "Nothing to avoid in the ingredient list",
                           "unsafe": "Contains something you avoid",
                           "unknown": "No ingredient list to check"}[verdict],
                "evidence_source": {"safe": "off", "unsafe": "kroger", "unknown": "none"}[verdict]})
        ingredients.append({"name": ing["name"], "role": ing.get("role") or "common",
                            "category": ing.get("category") or "other", "products": products})
    used = {p["store_id"] for i in ingredients for p in i["products"]}
    without = [i["name"] for i in ingredients if not any(p["verdict"] == "safe"
                                                       for p in i["products"])]
    return {
        "request": {"query": dish["name"], "zip": "45202", "avoid": avoid, "diet": diet},
        "dish": {"name": dish["name"], "cuisine": dish.get("cuisine") or "american",
                 "source": "catalog"},
        "stores": [s for s in STORES if s["store_id"] in used],
        "ingredients": ingredients,
        "summary": {"ingredients_total": len(ingredients),
                    "with_safe_product": len(ingredients) - len(without),
                    "without_safe_product": without},
        "meta": {"generated_at": "2026-11-10T12:00:00Z", "expires_at": "2026-11-11T12:00:00Z",
                 "providers_used": ["kroger"], "catalog_version": "synthetic",
                 "schema_version": "1.0"},
    }


def generate_a3(catalog: list[dict[str, Any]], gcfg: dict[str, Any], a1cfg: dict[str, Any],
                screen: Any) -> dict[str, list[dict]]:
    """{'dev': [...], 'test': [...]}: one synthetic result.json per catalog dish, split by
    dish exactly like A1 (the same test dishes), seeded."""
    from pantrypath_core.result import Result  # the contract: every case must validate

    rng = random.Random(gcfg["seed"])
    test_ids = held_out_dish_ids(catalog, a1cfg["seed"], a1cfg["test_fraction"])
    diets = list(gcfg["diet_weights"])
    splits: dict[str, list[dict]] = {"dev": [], "test": []}
    for dish in sorted(catalog, key=lambda d: d["dish_id"]):
        # allergens the dish really has: by ingredient category, or by name ("peanuts")
        relevant = sorted({a for i in dish["ingredients"]
                           for a in CATEGORY_ALLERGENS.get(i.get("category"), [])}
                          | {a for a, pattern in screen.allergens.items()
                             if any(pattern.search(i["name"].lower())
                                    for i in dish["ingredients"])})
        pool = relevant or [a.value for a in Allergen]
        avoid = sorted(rng.sample(pool, min(len(pool), rng.choice(gcfg["avoid_counts"]))))
        diet = rng.choices(diets, weights=[gcfg["diet_weights"][d] for d in diets])[0]
        result = synthetic_result(dish, avoid, diet, rng, screen, gcfg)
        Result.model_validate(result)
        split = "test" if dish["dish_id"] in test_ids else "dev"
        extras = {"servings": rng.choice(gcfg["servings"]), "tweaks": [],
                  "unsupported": rng.sample(gcfg["unsupported"], 1)
                  if rng.random() < float(gcfg["unsupported_share"]) else []}
        splits[split].append({
            "case_id": f"a3-{dish['dish_id']}", "dish_id": dish["dish_id"], "split": split,
            "result": result, "extras": extras,
            "slices": {"cuisine": result["dish"]["cuisine"], "diet": diet,
                       "avoid_count": len(avoid),
                       "without_safe": min(len(result["summary"]["without_safe_product"]), 3)}})
    return splits


def a3_path(ecfg: Config, split: str) -> Path:
    return ecfg.path(ecfg["a3"]["output_dir"]) / f"a3_{split}.jsonl"


def write_a3(pcfg: Config, ecfg: Config) -> dict[str, Any]:
    from pantrypath_ml.answer import SubstituteCheck

    screen = SubstituteCheck(config.load("answer"), config.load("parse_request"))
    splits = generate_a3(load_catalog(pcfg), ecfg["a3"], ecfg["a1"], screen)
    for split, cases in splits.items():
        write_jsonl(a3_path(ecfg, split), cases)
    summary = {split: len(cases) for split, cases in splits.items()}
    log.info("A3 evaluation sets written: %s", summary)
    return summary


def load_a3(ecfg: Config, split: str, *, allow_test: bool = False) -> list[dict[str, Any]]:
    _guard_test(split, allow_test)
    path = a3_path(ecfg, split)
    if not path.is_file():
        raise DataUnavailableError(f"{path} not found; run python -m pantrypath_ml.evalsets "
                                   "generate-a3")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pantrypath_ml.evalsets",
                                     description="Generate the A1 and A3 evaluation sets")
    parser.add_argument("command", choices=["generate-a1", "generate-a3", "convert-a1-csv"])
    parser.add_argument("--csv", type=Path, help="convert-a1-csv: the draft CSV")
    args = parser.parse_args(argv)
    setup_logging()
    if args.command == "convert-a1-csv":
        if not args.csv:
            parser.error("convert-a1-csv needs --csv")
        print(json.dumps(convert_a1_csv(config.pipeline_config(), config.load("evalsets"),
                                        args.csv)))
        return 0
    write = write_a1 if args.command == "generate-a1" else write_a3
    print(json.dumps(write(config.pipeline_config(), config.load("evalsets"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
