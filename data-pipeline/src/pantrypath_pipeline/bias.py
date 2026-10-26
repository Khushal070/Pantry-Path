"""Bias report: does the pipeline serve some groups worse than others? (data slicing)

Pantry Path trains no model in this phase, so we slice the two things that decide:

1. The dish catalog, by cuisine. Before = a plain top 100 by popularity (mostly
   American or untagged); after = the cuisine quotas in config (the mitigation).
   We also compare how much data backs each cuisine's dishes.
2. diet_rules, the safe / unsafe / unknown classifier (our "model"), across product
   slices: category group, store vs national brand, popularity. Ground truth comes
   from Open Food Facts label claims plus clear facts (animal categories, the gluten
   allergen tag) and is used for evaluation only, never by the rules.
   Before = the rules see allergen tags only; after = tags + ingredient text + name
   and category checks (the mitigation).

Per-slice metrics use Fairlearn's MetricFrame. Findings are written from the
numbers, so a rerun on new data stays truthful.
"""

import json
import logging
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd
from fairlearn.metrics import MetricFrame, count
from pydantic import ValidationError

from pantrypath_core import diet_rules
from pantrypath_core.catalog import Catalog
from pantrypath_core.diet_rules import Allergen, Diet, ProductEvidence, Verdict
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import DataCorruptError, DataUnavailableError, PipelineError

log = logging.getLogger(__name__)

EvidenceFn = Callable[[Mapping[str, Any]], ProductEvidence]
AssessFn = Callable[[ProductEvidence, Iterable[Allergen], Diet], Verdict]

# ---------------------------------------------------------------- ground truth

# The users we evaluate: (allergens to avoid, diet)
PROFILES: dict[str, tuple[frozenset[Allergen], Diet]] = {
    "vegan": (frozenset(), Diet.vegan),
    "vegetarian": (frozenset(), Diet.vegetarian),
    "gluten_free": (frozenset({Allergen.gluten}), Diet.none),
}

# Label claims that make a product safe for a profile. Vegan implies vegetarian;
# Open Food Facts spells the gluten-free claim en:no-gluten.
POSITIVE_LABELS = {
    "vegan": frozenset({"en:vegan"}),
    "vegetarian": frozenset({"en:vegetarian", "en:vegan"}),
    "gluten_free": frozenset({"en:no-gluten", "en:gluten-free"}),
}

MEAT_FISH_CATEGORIES = frozenset({
    "en:meats", "en:meats-and-their-products", "en:poultries", "en:chickens",
    "en:prepared-meats", "en:sausages", "en:hams", "en:bacon", "en:dried-meats",
    "en:fishes", "en:fishes-and-their-products", "en:canned-fishes", "en:tunas",
    "en:seafood", "en:shrimps", "en:crustaceans",
})
DAIRY_EGG_HONEY_CATEGORIES = frozenset({
    "en:dairies", "en:milks", "en:cheeses", "en:yogurts", "en:fermented-milk-products",
    "en:creams", "en:butters", "en:ice-creams", "en:eggs", "en:chicken-eggs",
    "en:honeys", "en:bee-products",
})
NON_VEGAN_CATEGORIES = MEAT_FISH_CATEGORIES | DAIRY_EGG_HONEY_CATEGORIES
GLUTEN_ALLERGEN_TAGS = frozenset({"en:gluten", "en:wheat"})

# Category tags with these words describe plant-based versions ("en:dairy-substitutes",
# "en:vegetarian-sausages"): never an animal fact, and a slice of their own
ANIMAL_FREE_MARKERS = (
    "dairy-substitut", "milk-substitut", "cheese-substitut", "egg-substitut",
    "meat-substitut", "alternative", "analogue", "imitation", "meatless", "meat-free",
    "non-dairy", "dairy-free", "vegan", "vegetarian", "veggie",
)

# Names or brands that contradict an animal or gluten fact: "Veggie burger" filed under
# en:meats, "Gluten-free nuggets" with a gluten tag. The category or tag is likely wrong,
# so such products are left out as conflicts instead of counting as unsafe.
NAME_ANIMAL_FREE = re.compile(
    r"\b(vegan|vegetarian|veggie|plant[- ]?based|meatless|meat[- ]free|dairy[- ]free"
    r"|non[- ]dairy|alternative|tofu|tempeh|seitan|jackfruit|seaweed|nori|kelp|algae"
    r"|(almond|cashew|oat|soy|rice|coconut|hemp|pea)\s+(milk|yogh?urt|creamer|cheese))\b"
)
NAME_GLUTEN_FREE = re.compile(r"\bgluten[- ]?free\b")

# ---------------------------------------------------------------- slices

# First match wins, so specific groups come before broad ones
CATEGORY_GROUPS: list[tuple[str, frozenset[str]]] = [
    ("meat_seafood", MEAT_FISH_CATEGORIES),
    ("dairy_eggs", DAIRY_EGG_HONEY_CATEGORIES - {"en:honeys", "en:bee-products"}),
    ("meals", frozenset({
        "en:meals", "en:soups", "en:pizzas", "en:pizzas-pies-and-quiches", "en:sandwiches",
        "en:entrees", "en:frozen-ready-made-meals",
    })),
    ("snacks_sweets", frozenset({
        "en:snacks", "en:sweet-snacks", "en:salty-snacks", "en:salted-snacks",
        "en:confectioneries", "en:chocolates", "en:chocolate-candies", "en:candies",
        "en:biscuits-and-cakes", "en:biscuits", "en:cakes", "en:desserts", "en:frozen-desserts",
    })),
    ("bakery_cereals", frozenset({
        "en:breads", "en:pastas", "en:breakfast-cereals", "en:cereals-and-potatoes",
        "en:cereals-and-their-products", "en:flours", "en:rices",
    })),
    ("beverages", frozenset({
        "en:beverages", "en:beverages-and-beverages-preparations", "en:alcoholic-beverages",
        "en:waters", "en:coffees", "en:teas", "en:plant-based-beverages",
        "en:carbonated-drinks", "en:sodas",
    })),
    ("pantry_condiments", frozenset({
        "en:condiments", "en:sauces", "en:spreads", "en:dips", "en:fats", "en:vegetable-fats",
        "en:vegetable-oils", "en:sweeteners", "en:honeys", "en:bee-products", "en:groceries",
        "en:cooking-helpers",
    })),
    ("produce_legumes_nuts", frozenset({
        "en:fruits-and-vegetables-based-foods", "en:fruits-based-foods",
        "en:vegetables-based-foods", "en:fruits", "en:vegetables", "en:legumes",
        "en:legumes-and-their-products", "en:nuts", "en:nuts-and-their-products", "en:seeds",
        "en:dried-fruits",
    })),
]
GROUP_ORDER = ["plant_based_substitutes", *(g for g, _ in CATEGORY_GROUPS), "other",
               "uncategorized"]
BRAND_ORDER = ["store_brand", "national_brand", "no_brand"]
SLICINGS = {
    "category_group": "category group",
    "brand_type": "brand type (store vs national)",
    "popularity_tier": "popularity (Open Food Facts scans)",
}

# ---------------------------------------------------------------- before / after


def _tags_only(ev: ProductEvidence) -> ProductEvidence:
    return ev.model_copy(update={"ingredients_text": "", "name": "", "brand": "",
                                 "categories": []})


def _tags_and_text(ev: ProductEvidence) -> ProductEvidence:
    return ev.model_copy(update={"name": "", "brand": "", "categories": []})


def _full(ev: ProductEvidence) -> ProductEvidence:
    return ev


MODES: dict[str, Callable[[ProductEvidence], ProductEvidence]] = {
    "tags_only": _tags_only,
    "tags_and_text": _tags_and_text,
    "full": _full,
}
MODE_LABELS = {
    "tags_only": "allergen and trace tags only (before)",
    "tags_and_text": "tags + ingredient text",
    "full": "tags + ingredient text + name and category checks (after, used by the pipeline)",
}
MODE_NAMES = {"tags_only": "tags only", "tags_and_text": "+ text", "full": "full"}
BEFORE, AFTER = "tags_only", "full"

# ---------------------------------------------------------------- metrics
# y_true holds "safe" / "unsafe" ("" when there is no ground truth); y_pred holds verdicts.


def _arrays(y_true: Any, y_pred: Any) -> tuple[np.ndarray, np.ndarray]:
    return np.asarray(y_true, dtype=object), np.asarray(y_pred, dtype=object)


def unknown_rate(y_true: Any, y_pred: Any) -> float:
    """Share of verdicts the rules could not decide."""
    _, y_pred = _arrays(y_true, y_pred)
    return float(np.mean(y_pred == "unknown")) if len(y_pred) else math.nan


def accuracy_decided(y_true: Any, y_pred: Any) -> float:
    """Of the decided verdicts (safe or unsafe), the share that matches the ground truth."""
    y_true, y_pred = _arrays(y_true, y_pred)
    decided = y_pred != "unknown"
    return float(np.mean(y_pred[decided] == y_true[decided])) if decided.any() else math.nan


def false_safe_rate(y_true: Any, y_pred: Any) -> float:
    """Of the products known to be unsafe, the share marked safe: the dangerous error."""
    y_true, y_pred = _arrays(y_true, y_pred)
    unsafe = y_true == "unsafe"
    return float(np.mean(y_pred[unsafe] == "safe")) if unsafe.any() else math.nan


def safe_found_rate(y_true: Any, y_pred: Any) -> float:
    """Of the products known to be safe, the share the rules confirm as safe."""
    y_true, y_pred = _arrays(y_true, y_pred)
    safe = y_true == "safe"
    return float(np.mean(y_pred[safe] == "safe")) if safe.any() else math.nan


def _n_decided(y_true: Any, y_pred: Any) -> int:
    return int(np.sum(_arrays(y_true, y_pred)[1] != "unknown"))


def _n_safe(y_true: Any, y_pred: Any) -> int:
    return int(np.sum(_arrays(y_true, y_pred)[0] == "safe"))


def _n_unsafe(y_true: Any, y_pred: Any) -> int:
    return int(np.sum(_arrays(y_true, y_pred)[0] == "unsafe"))


RATES = {
    "unknown_rate": unknown_rate,
    "accuracy_decided": accuracy_decided,
    "false_safe_rate": false_safe_rate,
    "safe_found_rate": safe_found_rate,
}
COUNTS = {"n": count, "n_decided": _n_decided, "n_safe": _n_safe, "n_unsafe": _n_unsafe}
# The count each rate is computed over: a gap needs enough of it in both groups
DENOMINATOR = {"unknown_rate": "n", "accuracy_decided": "n_decided",
               "false_safe_rate": "n_unsafe", "safe_found_rate": "n_safe"}
COVERAGE_RATES = ("unknown_rate",)
CORRECTNESS_RATES = tuple(RATES)

# ---------------------------------------------------------------- row helpers


def _as_set(values: Any) -> set[str]:
    if values is None or (isinstance(values, float) and math.isnan(values)):
        return set()
    if isinstance(values, str):
        return {values}
    return {str(v) for v in values}


def _has_animal_free_marker(categories: set[str]) -> bool:
    return any(marker in tag for tag in categories for marker in ANIMAL_FREE_MARKERS)


def ground_truth(row: Mapping[str, Any]) -> dict[str, str | None]:
    """Evaluation label per profile: 'safe', 'unsafe', 'conflict' (left out) or None.

    Safe = the product carries the label claim; unsafe = a clear fact (an animal
    category, or the gluten allergen tag). Categories for plant-based versions are never
    animal facts. A fact contradicted by a label claim, or by the product name or brand,
    is a conflict.
    """
    labels = _as_set(row.get("labels_tags"))
    categories = _as_set(row.get("categories_tags"))
    allergens = _as_set(row.get("allergens_tags"))
    name = f"{row.get('product_name') or ''} {row.get('brands') or ''}".lower()
    animal_free = _has_animal_free_marker(categories)
    negative = {
        "vegan": not animal_free and bool(categories & NON_VEGAN_CATEGORIES),
        "vegetarian": not animal_free and bool(categories & MEAT_FISH_CATEGORIES),
        "gluten_free": bool(allergens & GLUTEN_ALLERGEN_TAGS),
    }
    name_says_free = {
        "vegan": bool(NAME_ANIMAL_FREE.search(name)),
        "vegetarian": bool(NAME_ANIMAL_FREE.search(name)),
        "gluten_free": bool(NAME_GLUTEN_FREE.search(name)),
    }
    truth: dict[str, str | None] = {}
    for profile, claims in POSITIVE_LABELS.items():
        positive = bool(labels & claims)
        if negative[profile] and (positive or name_says_free[profile]):
            truth[profile] = "conflict"
        elif positive:
            truth[profile] = "safe"
        elif negative[profile]:
            truth[profile] = "unsafe"
        else:
            truth[profile] = None
    return truth


def category_group(categories: Any) -> str:
    """Map Open Food Facts category tags to one of ~10 broad groups."""
    tags = _as_set(categories) - {"en:undefined"}
    if not tags:
        return "uncategorized"
    if _has_animal_free_marker(tags):
        return "plant_based_substitutes"
    for group, members in CATEGORY_GROUPS:
        if tags & members:
            return group
    return "other"


def normalize_brand(name: Any) -> str:
    return " ".join(str(name).replace("’", "'").lower().split())


def brand_type(brands: str | None, store_brands: frozenset[str]) -> str:
    """store_brand when any listed brand is a retailer's own label, else national_brand."""
    names = [normalize_brand(b) for b in (brands or "").split(",")]
    names = [n for n in names if n]
    if not names:
        return "no_brand"
    for name in names:
        if name in store_brands or any(name.startswith(s + " ") for s in store_brands):
            return "store_brand"
    return "national_brand"


def tier_labels(bins: list[int]) -> list[str]:
    """[0, 1, 10] -> ['0 scans', '1-9 scans', '10+ scans']"""
    pairs = zip(bins, bins[1:], strict=False)
    labels = [f"{lo}" if hi - lo == 1 else f"{lo}-{hi - 1}" for lo, hi in pairs]
    return [f"{label} scans" for label in [*labels, f"{bins[-1]}+"]]


def popularity_tier(scans: Any, bins: list[int]) -> str:
    try:
        n = int(scans or 0)
    except (TypeError, ValueError):
        n = 0
    labels = tier_labels(bins)
    for label, upper in zip(labels, bins[1:], strict=False):
        if n < upper:
            return label
    return labels[-1]


# ---------------------------------------------------------------- reading the snapshot

ROW_COLUMNS = ("barcode", "product_name", "brands", "categories_tags", "allergens_tags",
               "traces_tags", "ingredients_text", "labels_tags", "unique_scans_n")
OPTIONAL_COLUMNS = ("ingredients_lang",)  # read by off_lookup.evidence_from_row if present


def _sql_list(values: Iterable[str]) -> str:
    return "[" + ", ".join("'" + v.replace("'", "''") + "'" for v in sorted(values)) + "]"


# Rows that may have a ground truth (exact rules are applied in Python afterwards)
LABELLED_FILTER = (
    f"list_has_any(labels_tags, {_sql_list(set().union(*POSITIVE_LABELS.values()))})"
    f" OR list_has_any(categories_tags, {_sql_list(NON_VEGAN_CATEGORIES)})"
    f" OR list_has_any(allergens_tags, {_sql_list(GLUTEN_ALLERGEN_TAGS)})"
)


def load_rows(path: Path, where: str = "TRUE", limit: int = 0,
              seed: int = 42) -> list[dict[str, Any]]:
    """Products from the snapshot in a fixed pseudo-random order (md5 of barcode + seed).

    Ordering by a hash instead of random sampling picks the same products on every run
    and machine, so the numbers only move when the data does. limit=0 reads all rows.
    """
    try:
        with duckdb.connect() as con:
            present = {r[0] for r in con.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()}
            missing = [c for c in ROW_COLUMNS if c not in present]
            if missing:
                raise DataCorruptError(f"{path} has no columns {missing}: rerun filter-off")
            columns = [*ROW_COLUMNS, *(c for c in OPTIONAL_COLUMNS if c in present)]
            sql = (f"SELECT {', '.join(columns)} FROM read_parquet(?) WHERE {where} "
                   f"ORDER BY md5(coalesce(barcode, '') || ':{int(seed)}')")
            if limit:
                sql += f" LIMIT {int(limit)}"
            cursor = con.execute(sql, [str(path)])
            return [dict(zip(columns, values, strict=True)) for values in cursor.fetchall()]
    except duckdb.Error as exc:
        raise DataCorruptError(
            f"Could not read {path}: {exc}. Rerun filter-off or `dvc pull` the snapshot."
        ) from exc


def _row_count(path: Path) -> int:
    with duckdb.connect() as con:
        return con.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()[0]


# ---------------------------------------------------------------- held-out products
# diet_rules was tuned by looking at every labelled product in the 2026-10-24 snapshot,
# so no product in it is held out. Its barcodes are frozen in bias.seen_products; a
# product a later snapshot adds was never seen, so the labelled ones among them form a
# held-out set that grows with every weekly run.


def _quoted(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def seen_info(path: Path) -> dict[str, Any] | None:
    """How many barcodes are frozen as seen, and when the last freeze was."""
    if not path.is_file():
        return None
    with duckdb.connect() as con:
        n, last = con.execute(
            "SELECT count(*), max(frozen_on) FROM read_parquet(?)", [str(path)]).fetchone()
    return {"seen_products": int(n), "frozen_on": str(last) if last else None}


def held_out_filter(seen: Path) -> str:
    """Labelled products whose barcode diet_rules has never been tuned on."""
    return (f"({LABELLED_FILTER}) AND barcode NOT IN "
            f"(SELECT barcode FROM read_parquet({_quoted(seen)}))")


def run_freeze_seen(cfg: Config, **_: Any) -> dict[str, Any]:
    """Step freeze-seen-products: mark every barcode in the current snapshot as seen.

    Run it by hand after changing diet_rules while looking at products from the current
    snapshot. Barcodes frozen earlier keep their date; only new ones are added.
    """
    snapshot = cfg.path(cfg["off"]["output"])
    if not snapshot.is_file():
        raise DataUnavailableError(f"{snapshot} not found: run filter-off or `dvc pull` it")
    seen = cfg.path(cfg["bias"]["seen_products"])
    seen.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).date().isoformat()
    new_file = seen.with_name(seen.name + ".tmp")
    with duckdb.connect() as con:
        old = (f"SELECT barcode, frozen_on FROM read_parquet({_quoted(seen)})"
               if seen.is_file() else "SELECT NULL::VARCHAR AS barcode, NULL::DATE AS frozen_on "
                                      "WHERE FALSE")
        con.execute(f"""
            COPY (
                WITH old AS ({old})
                SELECT * FROM old
                UNION ALL
                SELECT DISTINCT barcode, DATE '{today}' FROM read_parquet({_quoted(snapshot)})
                WHERE barcode IS NOT NULL AND barcode NOT IN (SELECT barcode FROM old)
                ORDER BY barcode
            ) TO {_quoted(new_file)} (FORMAT parquet, COMPRESSION zstd)""")
    new_file.replace(seen)
    info = seen_info(seen)
    log.info("bias: %d barcodes frozen as seen in %s", info["seen_products"], seen)
    return {**info, "file": str(seen)}


# ---------------------------------------------------------------- evaluation


@dataclass
class _Evaluator:
    """Runs the rules on each product, profile and mode, and remembers the verdicts."""

    evidence_fn: EvidenceFn
    assess_fn: AssessFn
    store_brands: frozenset[str]
    bins: list[int]
    verdicts: dict[tuple[str, str, str], str] = field(default_factory=dict)
    skipped: int = 0

    def records(self, rows: list[dict[str, Any]], sample: str,
                with_truth: bool) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                out.extend(self._row_records(row, sample, with_truth))
            except NotImplementedError as exc:
                raise PipelineError(
                    "diet_rules is not implemented yet (pantrypath_core.diet_rules.assess)"
                ) from exc
            except Exception as exc:  # one malformed product must not sink the report
                self.skipped += 1
                if self.skipped <= 3:
                    log.warning("Skipping product %s: %r", row.get("barcode"), exc)
        return out

    def _row_records(self, row: Mapping[str, Any], sample: str,
                     with_truth: bool) -> list[dict[str, Any]]:
        truth = ground_truth(row) if with_truth else {}
        profiles = [p for p in PROFILES if not with_truth or truth[p] in ("safe", "unsafe")]
        if not profiles:
            return []
        evidence = self.evidence_fn(row)
        slices = {
            "category_group": category_group(row.get("categories_tags")),
            "brand_type": brand_type(row.get("brands"), self.store_brands),
            "popularity_tier": popularity_tier(row.get("unique_scans_n"), self.bins),
        }
        out = []
        for profile in profiles:
            avoid, diet = PROFILES[profile]
            for mode, ablate in MODES.items():
                key = (str(row.get("barcode")), profile, mode)
                if key not in self.verdicts:
                    verdict = self.assess_fn(ablate(evidence), avoid, diet).verdict
                    self.verdicts[key] = getattr(verdict, "value", verdict)
                out.append({"sample": sample, "barcode": key[0], "profile": profile,
                            "mode": mode, "truth": truth.get(profile) or "",
                            "verdict": self.verdicts[key], **slices})
        return out


def _rates(frame: pd.DataFrame, names: Iterable[str]) -> dict[str, float]:
    y_true, y_pred = frame["truth"], frame["verdict"]
    out: dict[str, float] = {"n": len(frame)}
    for name in names:
        out[name] = RATES[name](y_true, y_pred)
    if names is CORRECTNESS_RATES:
        out.update({c: fn(y_true, y_pred) for c, fn in COUNTS.items() if c != "n"})
    return out


def slice_metrics(frame: pd.DataFrame, feature: str, min_size: int,
                  rates: tuple[str, ...] = CORRECTNESS_RATES) -> dict[str, Any]:
    """Fairlearn MetricFrame of the rule metrics for each group of one slicing feature.

    Gaps (difference = max - min, ratio = min / max) only compare groups with at least
    min_size items in the metric's denominator, so tiny groups can't fake a disparity.
    """
    if frame.empty:
        return {"by_group": {}, "gaps": {}}
    y_true = frame["truth"].to_numpy(dtype=object)
    y_pred = frame["verdict"].to_numpy(dtype=object)
    groups = pd.Series(frame[feature].to_numpy(dtype=object), name=feature)
    counts = COUNTS if rates == CORRECTNESS_RATES else {"n": count}
    mf = MetricFrame(metrics={**counts, **{r: RATES[r] for r in rates}},
                     y_true=y_true, y_pred=y_pred, sensitive_features=groups)
    by_group = mf.by_group

    gaps: dict[str, Any] = {}
    for rate in rates:
        eligible = [g for g, n in by_group[DENOMINATOR[rate]].items() if n >= min_size]
        if len(eligible) < 2:
            continue
        mask = groups.isin(eligible).to_numpy()
        mf_rate = MetricFrame(metrics={rate: RATES[rate]}, y_true=y_true[mask],
                              y_pred=y_pred[mask], sensitive_features=groups[mask])
        values = mf_rate.by_group[rate].dropna()
        if len(values) < 2:
            continue
        gaps[rate] = {
            "difference": mf_rate.difference()[rate],
            "ratio": mf_rate.ratio()[rate],
            "max_group": str(values.idxmax()), "max": values.max(),
            "min_group": str(values.idxmin()), "min": values.min(),
            "groups_compared": len(values),
        }
    return {"by_group": {str(g): {**row.to_dict(), **{c: int(row[c]) for c in counts}}
                         for g, row in by_group.iterrows()},
            "gaps": gaps}


def _truth_counts(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counts = {p: Counter() for p in PROFILES}
    for row in rows:
        for profile, label in ground_truth(row).items():
            if label:
                counts[profile][label] += 1
    return {p: {"safe": c["safe"], "unsafe": c["unsafe"], "conflict_excluded": c["conflict"]}
            for p, c in counts.items()}


def products_section(cfg: Config, evidence_fn: EvidenceFn, assess_fn: AssessFn) -> dict:
    """Part 2: diet_rules metrics overall and per slice, before and after mitigation."""
    settings = cfg["bias"]
    path = cfg.path(cfg["off"]["output"])
    if not path.is_file():
        raise DataUnavailableError(
            f"Open Food Facts snapshot {path} not found: run download-off and filter-off "
            "first, or `dvc pull` it"
        )
    seed = int(settings.get("seed", 42))
    min_size = int(settings.get("min_slice_size", 30))
    bins = [int(b) for b in settings.get("popularity_bins", [0, 1, 10])]

    started = time.monotonic()
    coverage_rows = load_rows(path, limit=int(settings.get("coverage_sample_size", 20000)),
                              seed=seed)
    labelled_rows = load_rows(path, where=LABELLED_FILTER,
                              limit=int(settings.get("labelled_sample_size", 40000)), seed=seed)
    seen_path = cfg.path(settings["seen_products"]) if settings.get("seen_products") else None
    seen = seen_info(seen_path) if seen_path else None
    held_out_rows = (load_rows(path, where=held_out_filter(seen_path), seed=seed)
                     if seen else [])
    log.info("bias: read %d coverage, %d labelled and %d held-out products in %.1fs",
             len(coverage_rows), len(labelled_rows), len(held_out_rows),
             time.monotonic() - started)

    evaluator = _Evaluator(
        evidence_fn, assess_fn,
        frozenset(normalize_brand(b) for b in settings.get("store_brands", [])), bins,
    )
    started = time.monotonic()
    records = (evaluator.records(coverage_rows, "coverage", with_truth=False)
               + evaluator.records(labelled_rows, "labelled", with_truth=True)
               + evaluator.records(held_out_rows, "held_out", with_truth=True))
    total = len(coverage_rows) + len(labelled_rows) + len(held_out_rows)
    if total and evaluator.skipped > 0.01 * total:
        raise PipelineError(
            f"{evaluator.skipped} of {total} products could not be evaluated; see the log"
        )
    log.info("bias: %d verdicts in %.1fs", len(evaluator.verdicts), time.monotonic() - started)

    frame = pd.DataFrame.from_records(records, columns=[
        "sample", "barcode", "profile", "mode", "truth", "verdict", *SLICINGS])
    coverage = frame[frame["sample"] == "coverage"]
    labelled = frame[frame["sample"] == "labelled"]
    held = frame[frame["sample"] == "held_out"]
    held_out: dict[str, Any] = {
        "status": "ok" if seen else "no_seen_list",
        **(seen or {}),
        "products": len(held_out_rows),
        "truth_counts": _truth_counts(held_out_rows),
        "correctness": {
            mode: {"pooled": _rates(held[held["mode"] == mode], CORRECTNESS_RATES),
                   **{p: _rates(held[(held["mode"] == mode) & (held["profile"] == p)],
                                CORRECTNESS_RATES) for p in PROFILES}}
            for mode in (BEFORE, AFTER)
        },
    }

    overall: dict[str, Any] = {"coverage": {}, "correctness": {}}
    for mode in MODES:
        cov, lab = coverage[coverage["mode"] == mode], labelled[labelled["mode"] == mode]
        overall["coverage"][mode] = {
            "pooled": unknown_rate(cov["truth"], cov["verdict"]),
            **{p: unknown_rate(None, cov.loc[cov["profile"] == p, "verdict"]) for p in PROFILES},
        }
        overall["correctness"][mode] = {
            "pooled": _rates(lab, CORRECTNESS_RATES),
            **{p: _rates(lab[lab["profile"] == p], CORRECTNESS_RATES) for p in PROFILES},
        }

    slices: dict[str, Any] = {}
    for slicing in SLICINGS:
        slices[slicing] = {
            "coverage": {
                mode: slice_metrics(coverage[coverage["mode"] == mode], slicing, min_size,
                                    COVERAGE_RATES)
                for mode in MODES
            },
            "correctness": {
                mode: slice_metrics(labelled[labelled["mode"] == mode], slicing, min_size)
                for mode in MODES
            },
            "coverage_by_profile": {
                profile: slice_metrics(
                    coverage[(coverage["mode"] == AFTER) & (coverage["profile"] == profile)],
                    slicing, min_size, COVERAGE_RATES)
                for profile in PROFILES
            },
            "correctness_by_profile": {
                profile: {
                    mode: slice_metrics(
                        labelled[(labelled["mode"] == mode) & (labelled["profile"] == profile)],
                        slicing, min_size)
                    for mode in (BEFORE, AFTER)
                }
                for profile in PROFILES
            },
        }

    names = {str(r.get("barcode")): (r.get("product_name") or "", r.get("brands") or "")
             for r in labelled_rows}
    after = labelled[labelled["mode"] == AFTER]

    def examples(truth: str, verdict: str) -> dict[str, list[dict[str, str]]]:
        """First five disagreements per user (the sample order is fixed, so are these)."""
        misses = after[(after["truth"] == truth) & (after["verdict"] == verdict)]
        return {
            profile: [{"barcode": b, "name": names[b][0], "brand": names[b][1],
                       "category_group": g}
                      for b, g in misses.loc[misses["profile"] == profile,
                                             ["barcode", "category_group"]].head(5).to_numpy()]
            for profile in PROFILES
        }

    return {
        "status": "ok",
        "source": path.name,
        "rows_total": _row_count(path),
        "coverage_products": len(coverage_rows),
        "labelled_products": len(labelled_rows),
        "skipped_products": evaluator.skipped,
        "truth_counts": _truth_counts(labelled_rows),
        "overall": overall,
        "held_out": held_out,
        "slices": slices,
        "false_safe_examples": examples("unsafe", "safe"),
        "false_unsafe_examples": examples("safe", "unsafe"),
    }


# ---------------------------------------------------------------- catalog (part 1)


def _mean_of(y_true: Any, y_pred: Any, values: Any) -> float:
    return float(np.mean(values)) if len(values) else math.nan


def _median_of(y_true: Any, y_pred: Any, values: Any) -> float:
    return float(np.median(values)) if len(values) else math.nan


def _hint_name(value: Any) -> str:
    name = str(value).strip().lower() if value is not None else ""
    return "unknown" if name in ("", "none", "null") else name


def plain_top100(pool_path: Path) -> dict[str, int] | None:
    """Cuisine mix of a plain top 100 by popularity (before quotas), from select-dishes."""
    if not pool_path.is_file():
        return None
    try:
        hints = json.loads(pool_path.read_text()).get("plain_top100_cuisine_hints")
    except (ValueError, AttributeError) as exc:
        log.warning("bias: could not read %s: %s", pool_path, exc)
        return None
    if isinstance(hints, dict):
        counts: Counter[str] = Counter()
        for name, n in hints.items():
            counts[_hint_name(name)] += int(n)
    elif isinstance(hints, list):
        counts = Counter(_hint_name(h.get("cuisine_hint") if isinstance(h, dict) else h)
                         for h in hints)
    else:
        return None
    return dict(counts) or None


def _dish_frame(groups: pd.Series, values: dict[str, np.ndarray]) -> dict[str, Any]:
    zeros = np.zeros(len(groups))
    metrics = {"dishes": count,
               **{name: _median_of if name.startswith("median") else _mean_of
                  for name in values}}
    mf = MetricFrame(metrics=metrics, y_true=zeros, y_pred=zeros, sensitive_features=groups,
                     sample_params={name: {"values": v} for name, v in values.items()})
    difference, ratio = mf.difference(), mf.ratio()
    return {
        "by_group": {str(g): {**row.to_dict(), "dishes": int(row["dishes"])}
                     for g, row in mf.by_group.iterrows()},
        "gaps": {name: {"difference": difference[name], "ratio": ratio[name]}
                 for name in values},
    }


def catalog_section(cfg: Config) -> dict[str, Any]:
    """Part 1: representation and data support per cuisine, before vs after the quotas."""
    quotas = {str(k): int(v) for k, v in cfg["catalog"]["quotas"].items()}
    section: dict[str, Any] = {
        "quotas": quotas,
        "before": plain_top100(cfg.path(cfg["foodcom"]["pool_output"])),
    }
    profiles_path = cfg.path(cfg["catalog"]["profiles_output"])
    if not profiles_path.is_file():
        return {**section, "status": "pending",
                "message": "the catalog needs an LLM run (refine-all, then assemble-catalog)"}
    try:
        catalog = Catalog.model_validate_json(profiles_path.read_text())
    except (ValidationError, ValueError) as exc:
        raise DataCorruptError(
            f"{profiles_path} is not a valid catalog ({exc}); rerun assemble-catalog"
        ) from exc
    dishes = catalog.dishes
    if not dishes:
        return {**section, "status": "pending", "message": "the catalog is empty"}

    cuisines = pd.Series([d.cuisine.value for d in dishes], name="cuisine")
    values = {
        "ingredients_per_dish": np.array([len(d.ingredients) for d in dishes], dtype=float),
        "core_per_dish": np.array(
            [sum(i.role.value == "core" for i in d.ingredients) for d in dishes], dtype=float),
        "llm_added_share": np.array(
            [sum(i.added_by_llm for i in d.ingredients) / len(d.ingredients) for d in dishes]),
        "median_recipes": np.array([d.recipe_count for d in dishes], dtype=float),
        "median_reviews": np.array([d.review_count for d in dishes], dtype=float),
    }
    by_cuisine = _dish_frame(cuisines, values)
    american = pd.Series(np.where(cuisines == "american", "american", "other cuisines"),
                         name="group")

    summary_path = cfg.path(cfg["paths"]["reports"]) / "catalog_summary.json"
    try:
        summary = json.loads(summary_path.read_text()) if summary_path.is_file() else None
    except ValueError:
        summary = None
    return {
        **section,
        "status": "ok",
        "catalog_version": catalog.catalog_version,
        "dishes": len(dishes),
        "after": dict(Counter(cuisines)),
        "per_cuisine": by_cuisine["by_group"],
        "gaps": by_cuisine["gaps"],
        "american_vs_other": _dish_frame(american, values)["by_group"],
        "summary": summary,
    }


# ---------------------------------------------------------------- findings


def _pct(x: float | None, digits: int | None = None) -> str:
    """0.78 -> '78%'; small values keep a decimal (0.013 -> '1.3%') unless digits is given."""
    if x is None:
        return "n/a"
    if digits is None:
        digits = 1 if 0 < abs(x) < 0.1 else 0
    return f"{x:.{digits}%}"


def _pts(x: float | None) -> str:
    """A difference between two rates, in percentage points."""
    if x is None:
        return "n/a"
    points = x * 100
    return f"{points:.1f} pts" if abs(points) < 10 else f"{points:.0f} pts"


def _label(group: str) -> str:
    return group.replace("_", " ")


def catalog_findings(c: dict[str, Any]) -> list[str]:
    out = []
    before = c.get("before")
    if before:
        total = sum(before.values())
        american, untagged = before.get("american", 0), before.get("unknown", 0)
        out.append(
            f"Catalog before mitigation: a plain top {total} by popularity is "
            f"{_pct((american + untagged) / total)} American or untagged ({american} tagged "
            f"American, {untagged} with no cuisine keyword)."
        )
    if c["status"] != "ok":
        out.append(f"Catalog after mitigation: pending; {c['message']}.")
        return out

    n, after, quotas = c["dishes"], c["after"], c["quotas"]
    out.append(
        f"Catalog after mitigation (cuisine quotas): {n} dishes, "
        f"{_pct(after.get('american', 0) / n)} American (target "
        f"{_pct(quotas.get('american', 0) / max(sum(quotas.values()), 1))})."
    )
    short = [f"{_label(k)} {after.get(k, 0)}/{q}" for k, q in quotas.items()
             if after.get(k, 0) < q]
    out.append("Every cuisine reached its quota." if not short
               else "Below quota: " + ", ".join(short) + ".")
    a = c["american_vs_other"].get("american")
    o = c["american_vs_other"].get("other cuisines")
    if a and o:
        out.append(
            f"Data behind each dish: other cuisines rest on a median of "
            f"{o['median_recipes']:.0f} Food.com recipes (American: {a['median_recipes']:.0f}); "
            f"{_pct(o['llm_added_share'])} of their ingredients were added by the LLM "
            f"(American: {_pct(a['llm_added_share'])})."
        )
    return out


def _worst_group(p: dict[str, Any], rate: str, min_size: int) -> dict[str, Any] | None:
    """The slice with the highest value of a rate (full rules), among large enough groups."""
    worst = None
    for slicing in SLICINGS:
        for group, m in p["slices"][slicing]["correctness"][AFTER]["by_group"].items():
            value, n = m.get(rate), m.get(DENOMINATOR[rate], 0)
            if value is not None and n >= min_size and (worst is None or value > worst["value"]):
                worst = {"slicing": SLICINGS[slicing], "group": group, "value": value, "n": n}
    return worst


def product_findings(p: dict[str, Any], min_size: int) -> list[str]:
    cov, cor = p["overall"]["coverage"], p["overall"]["correctness"]
    b, t, a = cor[BEFORE]["pooled"], cor["tags_and_text"]["pooled"], cor[AFTER]["pooled"]
    never_safe = " (tags alone never prove a product safe)" if b["safe_found_rate"] == 0 else ""
    out = [
        f"Coverage: on a fixed sample of {p['coverage_products']:,} products, diet_rules "
        f"could not decide {_pct(cov[BEFORE]['pooled'])} of verdicts from allergen tags "
        f"alone; reading the ingredient text brings this to "
        f"{_pct(cov['tags_and_text']['pooled'])}, and the name/category checks to "
        f"{_pct(cov[AFTER]['pooled'])}.",
        f"Dangerous errors: products known to be unsafe were marked safe "
        f"{_pct(b['false_safe_rate'], 1)} of the time with tags only, "
        f"{_pct(t['false_safe_rate'], 1)} with the ingredient text, and "
        f"{_pct(a['false_safe_rate'], 1)} with the full rules.",
        f"Usefulness: the rules confirm {_pct(b['safe_found_rate'])} of labelled-safe products "
        f"as safe before{never_safe} and {_pct(a['safe_found_rate'])} after; when they "
        f"decide, they match the labels {_pct(b['accuracy_decided'])} → "
        f"{_pct(a['accuracy_decided'])} of the time.",
    ]
    for slicing, label in SLICINGS.items():
        gap_before = p["slices"][slicing]["coverage"][BEFORE]["gaps"].get("unknown_rate")
        gap_after = p["slices"][slicing]["coverage"][AFTER]["gaps"].get("unknown_rate")
        if not gap_after:
            continue
        text = (f"By {label}, the unknown rate differs by up to "
                f"{_pts(gap_after['difference'])} between groups with the full rules "
                f"({_label(gap_after['max_group'])} {_pct(gap_after['max'])} vs "
                f"{_label(gap_after['min_group'])} {_pct(gap_after['min'])})")
        if gap_before:
            change = ("narrower than" if gap_after["difference"] < gap_before["difference"]
                      else "wider than" if gap_after["difference"] > gap_before["difference"]
                      else "the same as")
            text += f", {change} the {_pts(gap_before['difference'])} gap with tags only"
        group = gap_after["max_group"]
        per_user = {u: m["by_group"].get(group, {}).get("unknown_rate")
                    for u, m in p["slices"][slicing]["coverage_by_profile"].items()}
        per_user = {u: v for u, v in per_user.items() if v}
        if per_user:
            user = max(per_user, key=per_user.get)
            text += (f"; in {_label(group)} the {_label(user)} user is affected most "
                     f"({_pct(per_user[user])} unknown)")
        out.append(text + ".")
    worst = _worst_group(p, "false_safe_rate", min_size)
    if worst and worst["value"] > 0:
        out.append(
            f"Highest false-safe slice with the full rules: {_label(worst['group'])} "
            f"({worst['slicing']}), {_pct(worst['value'], 1)} of {worst['n']:,} "
            f"unsafe verdicts."
        )
    h = p.get("held_out") or {}
    if h.get("status") == "ok":
        if h["products"] >= min_size:
            pooled = h["correctness"][AFTER]["pooled"]
            out.append(
                f"Held out: on {h['products']:,} products added after the rules were frozen, "
                f"false safe is {_pct(pooled['false_safe_rate'], 1)} and accuracy when decided "
                f"{_pct(pooled['accuracy_decided'])}, against "
                f"{_pct(a['false_safe_rate'], 1)} and {_pct(a['accuracy_decided'])} on the "
                f"fixed sample.")
        else:
            out.append(
                f"Held out: {h['products']:,} labelled products added since the seen list was "
                f"frozen on {h['frozen_on']}; too few to score yet (the set grows weekly).")
    return out


def headline(p: dict[str, Any]) -> str:
    """One sentence for alerts and summaries: the mitigation effect and the widest gap left."""
    cov, cor = p["overall"]["coverage"], p["overall"]["correctness"]
    text = (f"Ingredient-text fallback and name/category checks cut diet_rules' unknown rate "
            f"from {_pct(cov[BEFORE]['pooled'])} to {_pct(cov[AFTER]['pooled'])} (false safe "
            f"{_pct(cor[BEFORE]['pooled']['false_safe_rate'], 1)} → "
            f"{_pct(cor[AFTER]['pooled']['false_safe_rate'], 1)})")
    gaps = [(SLICINGS[s], g) for s in SLICINGS
            if (g := p["slices"][s]["coverage"][AFTER]["gaps"].get("unknown_rate"))]
    if gaps:
        label, gap = max(gaps, key=lambda item: item[1]["difference"])
        text += (f"; the widest gap left is by {label}: {_label(gap['max_group'])} "
                 f"{_pct(gap['max'])} vs {_label(gap['min_group'])} {_pct(gap['min'])} unknown")
    return text + "."


def mitigation_notes(report: dict[str, Any]) -> list[str]:
    """Templated paragraphs for the mitigation and trade-off section."""
    notes = []
    c = report["catalog"]
    if c["status"] == "ok":
        before, n = c.get("before"), c["dishes"]
        share_after = c["after"].get("american", 0) / n
        text = (f"**Catalog: cuisine quotas** (`catalog.quotas`). The catalog is "
                f"{_pct(share_after)} American")
        if before:
            total = sum(before.values())
            share_before = (before.get("american", 0) + before.get("unknown", 0)) / total
            text += f", down from {_pct(share_before)} American or untagged in a plain top {total}"
        text += "."
        a = c["american_vs_other"].get("american")
        o = c["american_vs_other"].get("other cuisines")
        if a and o:
            costs = []
            if o["median_recipes"] < a["median_recipes"]:
                costs.append(f"backed by fewer source recipes (median {o['median_recipes']:.0f} "
                             f"vs {a['median_recipes']:.0f} for American dishes), so their "
                             f"ingredient shares rest on less data")
            if o["llm_added_share"] > a["llm_added_share"]:
                costs.append(f"with more LLM-added ingredients ({_pct(o['llm_added_share'])} "
                             f"vs {_pct(a['llm_added_share'])}), tagged `added_by_llm` for review")
            if costs:
                text += " Trade-off: the quotas pull in dishes " + "; and ".join(costs) + "."
            else:
                text += (" The added cuisines are backed by as much source data as the "
                         "American dishes, so the quotas cost no data quality here.")
        notes.append(text)
    else:
        notes.append(f"**Catalog: cuisine quotas** (`catalog.quotas`). Pending: {c['message']}.")

    p = report["products"]
    cov, cor = p["overall"]["coverage"], p["overall"]["correctness"]
    b, t, a = cor[BEFORE]["pooled"], cor["tags_and_text"]["pooled"], cor[AFTER]["pooled"]
    text = (f"**diet_rules: ingredient-text fallback and name/category checks.** The unknown "
            f"rate drops from {_pct(cov[BEFORE]['pooled'])} to {_pct(cov[AFTER]['pooled'])} "
            f"and labelled-safe products confirmed as safe rise from "
            f"{_pct(b['safe_found_rate'])} to {_pct(a['safe_found_rate'])}.")
    fs_b, fs_t, fs_a = b["false_safe_rate"], t["false_safe_rate"], a["false_safe_rate"]
    if None not in (fs_b, fs_a):
        if fs_a > fs_b:
            text += (f" Trade-off: deciding more products costs some safety: the false-safe "
                     f"rate rises from {_pct(fs_b, 1)} to {_pct(fs_a, 1)} "
                     f"({(fs_a - fs_b) * 1000:.0f} more per 1,000 unsafe products).")
        else:
            text += (f" The extra decisions cost no safety: the false-safe rate goes from "
                     f"{_pct(fs_b, 1)} to {_pct(fs_a, 1)}.")
    if None not in (fs_t, fs_a) and fs_t > fs_a:
        text += (f" Ingredient text alone would mark {_pct(fs_t, 1)} of unsafe products safe "
                 f"(e.g. cure-only bacon lists no meat); the name/category check brings this "
                 f"down to {_pct(fs_a, 1)}.")
    notes.append(text)
    notes.append(
        "**Not applied:** re-sampling, re-weighting and threshold tuning are levers for "
        "trained models; diet_rules has no training data and no score threshold. The "
        "equivalent safeguard is kept instead: unknown is never shown as safe, so a slice "
        "with more unknowns gets fewer suggestions, never less safe ones."
    )
    return notes


# ---------------------------------------------------------------- markdown

LIMITATIONS = [
    "**Not a held-out evaluation.** While diet_rules was being built, its keyword rules were "
    "adjusted after looking at mismatches with these same Open Food Facts vegan, vegetarian "
    "and gluten-free labels on a ~47k-product sample of this snapshot (for example, recall "
    "on vegan-labelled products rose from 91.3% to 92.7%). The fixed sample used here "
    "overlaps that data, so accuracy and safe-found numbers are likely optimistic. The fair "
    "estimate is the held-out check: products added to Open Food Facts after the snapshot "
    "the rules were tuned on (its barcodes are frozen in `bias.seen_products`). It starts "
    "empty and grows with each weekly snapshot; rerun `freeze-seen-products` whenever the "
    "rules are changed after looking at newer products, or the check stops being held out.",
    "**Label noise.** Open Food Facts claims are not always right: some products labelled "
    "vegan or gluten-free list eggs, chicken stock or wheat (see the disagreement tables). "
    "Such products count against the rules when the rules are actually right, and a "
    "category or tag error does the same in the other direction.",
    "Labels are self-reported packaging claims entered by Open Food Facts contributors. "
    "Brands that market to vegan or gluten-free shoppers carry them more often, so the "
    "labelled set is not a random sample of products.",
    "\"Unsafe\" is a proxy. Open Food Facts categories and tags have errors (meat-free "
    "burgers filed under meats); products whose name or brand contradicts the fact are "
    "left out, "
    "but some errors remain, so part of the false-safe rate is label noise. "
    "Gluten negatives come from the same allergen tags the rules "
    "read, so every mode scores close to perfect on them by construction. Vegan and vegetarian "
    "negatives come from categories, which only the full mode reads: its false-safe "
    "numbers on them are a lower bound.",
    "The snapshot only keeps products with ingredient or allergen data, and store products "
    "missing from Open Food Facts are not in it, so unknown rates at request time are higher.",
    "Store vs national brand uses a hand-made list of US store brands (`bias.store_brands`); "
    "regional store brands count as national.",
    "Popularity is Open Food Facts scans: a proxy for how common a product is, not sales.",
    "Catalog: \"before\" uses Food.com cuisine keywords, \"after\" the LLM's cuisine labels; "
    "untagged Food.com dishes were mostly American when checked by hand.",
]


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return [*lines, ""]


def _ordered(groups: Iterable[str], slicing: str, bins: list[int]) -> list[str]:
    order = {"category_group": GROUP_ORDER, "brand_type": BRAND_ORDER,
             "popularity_tier": tier_labels(bins)}[slicing]
    groups = list(groups)
    return [g for g in order if g in groups] + sorted(set(groups) - set(order))


def _catalog_md(c: dict[str, Any]) -> list[str]:
    lines = [
        "## 1. Dish catalog by cuisine", "",
        "Before: Food.com cuisine keywords for a plain top 100 by popularity (untagged = no "
        "cuisine keyword). After: the LLM's cuisine labels in the quota catalog "
        "(`catalog.quotas`, the mitigation).", "",
    ]
    before, after, quotas = c.get("before") or {}, c.get("after") or {}, c["quotas"]
    total_before, total_after = sum(before.values()), sum(after.values())

    def cell(counts: dict[str, int], key: str, total: int) -> str:
        return f"{counts.get(key, 0)} ({counts.get(key, 0) / total:.0%})" if total else "n/a"

    rows = [[_label(k), cell(before, k, total_before), q, cell(after, k, total_after)]
            for k, q in quotas.items()]
    if before.get("unknown"):
        rows.append(["untagged", cell(before, "unknown", total_before), "–", "–"])
    lines += _table(["Cuisine", "Plain top 100 (before)", "Quota", "Catalog (after)"], rows)
    if c["status"] != "ok":
        return [*lines, f"_Pending: {c['message']}._", ""]

    lines += ["Data behind each dish (Fairlearn `MetricFrame`, sensitive feature = cuisine):", ""]
    rows = []
    for cuisine in [*quotas, *sorted(set(c["per_cuisine"]) - set(quotas))]:
        m = c["per_cuisine"].get(cuisine)
        if m:
            rows.append([_label(cuisine), int(m["dishes"]), f"{m['ingredients_per_dish']:.1f}",
                         f"{m['core_per_dish']:.1f}", _pct(m["llm_added_share"]),
                         f"{m['median_recipes']:.0f}", f"{m['median_reviews']:.0f}"])
    lines += _table(["Cuisine", "Dishes", "Ingredients per dish", "Core per dish",
                     "Added by LLM", "Median recipes", "Median reviews"], rows)
    gaps = c["gaps"]
    lines += [
        "Largest gaps between cuisines: "
        f"{gaps['ingredients_per_dish']['difference']:.1f} ingredients per dish, "
        f"{_pts(gaps['llm_added_share']['difference'])} in LLM-added share, "
        f"{gaps['median_recipes']['difference']:.0f} in median source recipes.", "",
    ]
    return lines


def _held_out_md(h: dict[str, Any], min_size: int) -> list[str]:
    lines = ["### Held-out check: products added after the rules were frozen", ""]
    if h.get("status") != "ok":
        return lines + ["No list of seen products (`bias.seen_products`), so there is no "
                        "held-out check in this run. Create it with "
                        "`python -m pantrypath_pipeline freeze-seen-products`.", ""]
    lines += [
        f"diet_rules was tuned on the products whose {h['seen_products']:,} barcodes were "
        f"frozen as seen (last freeze {h['frozen_on']}). Products a later snapshot adds were never "
        "looked at, so the labelled ones among them are a true held-out test. The set "
        "starts empty and grows with every weekly snapshot.", "",
    ]
    if not h["products"]:
        return lines + ["**0 held-out products in this snapshot** (it is the frozen one).", ""]
    small = h["products"] < min_size
    lines.append(f"**{h['products']:,} held-out products** with a ground truth"
                 + (f" (fewer than {min_size}: too few to compare with the numbers above yet)"
                    if small else "") + ". Each cell: tags only → full rules.")
    lines.append("")
    cor = h["correctness"]
    rows = []
    for prof in [*PROFILES, "pooled"]:
        b, a = cor[BEFORE][prof], cor[AFTER][prof]
        rows.append(["all users" if prof == "pooled" else _label(prof), f"{int(a['n']):,}",
                     f"{_pct(b['unknown_rate'])} → {_pct(a['unknown_rate'])}",
                     f"{_pct(b['accuracy_decided'])} → {_pct(a['accuracy_decided'])}",
                     f"{_pct(b['false_safe_rate'], 1)} → {_pct(a['false_safe_rate'], 1)}",
                     f"{_pct(b['safe_found_rate'])} → {_pct(a['safe_found_rate'])}"])
    return lines + _table(["User", "Verdicts", "Unknown", "Accuracy when decided",
                           "False safe", "Safe found"], rows)


def _products_md(p: dict[str, Any], settings: dict[str, Any]) -> list[str]:
    min_size, bins = settings["min_slice_size"], settings["popularity_bins"]
    lines = [
        "## 2. diet_rules across product slices", "",
        "### How it was measured", "",
        f"- **Data:** the Open Food Facts US snapshot ({p['rows_total']:,} products with "
        f"ingredient or allergen data). Coverage uses a fixed sample of "
        f"{p['coverage_products']:,} products; correctness uses {p['labelled_products']:,} "
        f"products that may have a ground truth (seed {settings['seed']}: the same products "
        f"every run). This is a fixed sample, not a held-out test set (see Limitations); "
        f"the held-out check below scores only products the rules were never tuned on.",
        "- **Users:** vegan, vegetarian, and avoiding gluten.",
        "- **Ground truth (evaluation only, never read by the rules):** safe = the product "
        "carries the claim (`en:vegan`; `en:vegetarian` or `en:vegan`; `en:no-gluten`). "
        "Unsafe = a clear fact: an animal category (meat, fish, seafood; for vegans also "
        "dairy, eggs, honey) or the gluten allergen tag. A fact contradicted by a claim or by "
        "the product name or brand (\"veggie burger\" under meats, \"gluten-free\" with a "
        "gluten tag) is a conflict and left out; categories for plant-based versions "
        "(substitutes, alternatives, vegan, …) never count as animal.",
        "- **Before / after:** " + "; ".join(f"*{MODE_NAMES[m]}* = {label}"
                                            for m, label in MODE_LABELS.items()) + ".",
        "- **Metrics:** *unknown* = share the rules can't decide (never shown as safe); "
        "*accuracy* = agreement with the ground truth when decided; *false safe* = products "
        "known to be unsafe marked safe (the dangerous error); *safe found* = labelled-safe "
        "products confirmed safe.",
        f"- Groups with fewer than {min_size} verdicts are marked * and left out of gap numbers.",
        "",
    ]
    rows = [[_label(prof), f"{c['safe']:,}", f"{c['unsafe']:,}", f"{c['conflict_excluded']:,}"]
            for prof, c in p["truth_counts"].items()]
    lines += _table(["User", "Safe (label claim)", "Unsafe (fact)", "Conflicts left out"], rows)

    def chain(values: list[float | None], digits: int | None = None) -> str:
        return " → ".join(_pct(v, digits) for v in values)

    cov, cor = p["overall"]["coverage"], p["overall"]["correctness"]
    lines += ["### Overall", "", "Each cell: tags only → + ingredient text → full rules.", ""]
    rows = []
    for prof in [*PROFILES, "pooled"]:
        rows.append([
            "all users" if prof == "pooled" else _label(prof),
            chain([cov[m][prof] for m in MODES]),
            chain([cor[m][prof]["accuracy_decided"] for m in MODES]),
            chain([cor[m][prof]["false_safe_rate"] for m in MODES], 1),
            chain([cor[m][prof]["safe_found_rate"] for m in MODES]),
        ])
    lines += _table(["User", "Unknown (all products)", "Accuracy when decided",
                     "False safe", "Safe found"], rows)
    lines += _held_out_md(p.get("held_out") or {}, min_size)

    lines += ["### Coverage by slice (unknown rate, all users)", ""]
    for slicing, label in SLICINGS.items():
        by_mode = {m: p["slices"][slicing]["coverage"][m]["by_group"] for m in MODES}
        rows = []
        for group in _ordered(by_mode[AFTER], slicing, bins):
            n = int(by_mode[AFTER][group]["n"])
            star = "*" if n < min_size else ""
            rows.append([_label(group) + star, f"{n // len(PROFILES):,}",
                         chain([by_mode[m].get(group, {}).get("unknown_rate") for m in MODES])])
        lines += [f"**By {label}**", ""]
        lines += _table(["Group", "Products", "Unknown: tags → + text → full"], rows)

    lines += ["### Correctness by slice (labelled products, all users)", "",
              "Each cell: tags only → full rules.", ""]
    for slicing, label in SLICINGS.items():
        before = p["slices"][slicing]["correctness"][BEFORE]["by_group"]
        after = p["slices"][slicing]["correctness"][AFTER]["by_group"]
        rows = []
        for group in _ordered(after, slicing, bins):
            m_b, m_a = before.get(group, {}), after[group]
            star = "*" if m_a["n"] < min_size else ""
            rows.append([
                _label(group) + star, f"{int(m_a['n']):,}",
                chain([m_b.get("accuracy_decided"), m_a["accuracy_decided"]]),
                chain([m_b.get("false_safe_rate"), m_a["false_safe_rate"]], 1),
                chain([m_b.get("safe_found_rate"), m_a["safe_found_rate"]]),
            ])
        lines += [f"**By {label}**", ""]
        lines += _table(["Group", "Verdicts", "Accuracy", "False safe", "Safe found"], rows)

    lines += ["### Largest gaps between slices", "",
              "Fairlearn `MetricFrame.difference()` (max - min across groups), tags only → full "
              "rules; the groups named are the extremes with the full rules.", ""]

    def gap_cell(before: dict | None, after: dict | None) -> str:
        if not after:
            return "n/a"
        start = _pts(before["difference"]) if before else "n/a"
        return (f"{start} → {_pts(after['difference'])} ({_label(after['max_group'])} "
                f"{_pct(after['max'])} vs {_label(after['min_group'])} {_pct(after['min'])})")

    rows = []
    for slicing, label in SLICINGS.items():
        s = p["slices"][slicing]
        rows.append([
            label,
            gap_cell(s["coverage"][BEFORE]["gaps"].get("unknown_rate"),
                     s["coverage"][AFTER]["gaps"].get("unknown_rate")),
            gap_cell(s["correctness"][BEFORE]["gaps"].get("accuracy_decided"),
                     s["correctness"][AFTER]["gaps"].get("accuracy_decided")),
            gap_cell(s["correctness"][BEFORE]["gaps"].get("false_safe_rate"),
                     s["correctness"][AFTER]["gaps"].get("false_safe_rate")),
        ])
    lines += _table(["Slicing", "Unknown rate", "Accuracy when decided", "False safe"], rows)

    def example_rows(key: str) -> list[list[str]]:
        return [[_label(user), ex["name"].replace("|", "/") or "(no name)",
                 ex["brand"].replace("|", "/") or "–", _label(ex["category_group"]),
                 ex["barcode"]]
                for user, items in p[key].items() for ex in items]

    headers = ["User", "Product", "Brand", "Group", "Barcode"]
    if rows := example_rows("false_safe_examples"):
        lines += ["### Disagreements: ground truth unsafe, rules say safe", "",
                  "First five per user with the full rules (same every run). Some are real "
                  "misses (leads for diet_rules), some are wrong categories or tags in Open "
                  "Food Facts.", ""]
        lines += _table(headers, rows)
    if rows := example_rows("false_unsafe_examples"):
        lines += ["### Disagreements: labelled safe, rules say unsafe", "",
                  "First five per user. Many are label errors: products claiming vegan or "
                  "gluten-free while listing eggs, chicken stock or wheat.", ""]
        lines += _table(headers, rows)
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Bias report: data slicing", "",
        f"_Generated {report['generated_at']} by `python -m pantrypath_pipeline bias-report`. "
        "Every number and finding is recomputed on each run._", "",
        "Pantry Path trains no model in this phase. Two parts of the pipeline make decisions "
        "that could serve some groups worse than others:", "",
        "1. the **dish catalog**: which cuisines get dishes, and how much data backs each dish;",
        "2. **diet_rules**, which marks every product *safe*, *unsafe* or *unknown* for a user. "
        "It is the \"model\" evaluated here, on slices of products.", "",
        f"**In one line:** {report['headline']}", "",
        "## Findings", "",
        *[f"- {f}" for f in report["findings"]], "",
        *_catalog_md(report["catalog"]),
        *_products_md(report["products"], report["settings"]),
        "## 3. Mitigation and trade-offs", "",
        *[f"- {note}" for note in report["mitigation"]], "",
        "## 4. Limitations", "",
        *[f"- {item}" for item in LIMITATIONS],
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- step


def _jsonable(obj: Any) -> Any:
    """Plain JSON types; NaN and infinity become null, floats keep 4 decimals."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, bool | np.bool_):
        return bool(obj)
    if isinstance(obj, int | np.integer):
        return int(obj)
    if isinstance(obj, float | np.floating):
        value = float(obj)
        return round(value, 4) if math.isfinite(value) else None
    return obj


def _off_evidence_fn() -> EvidenceFn:
    try:
        from pantrypath_pipeline.off_lookup import evidence_from_row
    except ImportError as exc:
        raise PipelineError(
            "pantrypath_pipeline.off_lookup.evidence_from_row is missing: it turns a "
            "snapshot row into ProductEvidence"
        ) from exc
    return evidence_from_row


def run(cfg: Config, evidence_fn: EvidenceFn | None = None,
        assess_fn: AssessFn | None = None, **_: Any) -> dict[str, Any]:
    """Step bias-report: slice the catalog and diet_rules; write the JSON and Markdown report."""
    settings = cfg["bias"]
    products = products_section(cfg, evidence_fn or _off_evidence_fn(),
                                assess_fn or diet_rules.assess)
    report = _jsonable({
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "settings": {
            "seed": int(settings.get("seed", 42)),
            "coverage_sample_size": int(settings.get("coverage_sample_size", 20000)),
            "labelled_sample_size": int(settings.get("labelled_sample_size", 40000)),
            "min_slice_size": int(settings.get("min_slice_size", 30)),
            "popularity_bins": [int(b) for b in settings.get("popularity_bins", [0, 1, 10])],
            "profiles": list(PROFILES),
            "modes": MODE_LABELS,
        },
        "catalog": catalog_section(cfg),
        "products": products,
    })
    min_size = report["settings"]["min_slice_size"]
    report["headline"] = headline(report["products"])
    report["findings"] = (catalog_findings(report["catalog"])
                          + product_findings(report["products"], min_size))
    report["mitigation"] = mitigation_notes(report)

    json_path, md_path = cfg.path(settings["output_json"]), cfg.path(settings["output_md"])
    for path in (json_path, md_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2) + "\n")
    md_path.write_text(render_markdown(report))
    log.info("bias: wrote %s and %s", json_path, md_path)

    overall = report["products"]["overall"]
    cov, cor = overall["coverage"], overall["correctness"]
    return {
        "coverage_products": report["products"]["coverage_products"],
        "labelled_products": report["products"]["labelled_products"],
        "held_out_products": report["products"]["held_out"]["products"],
        "catalog": report["catalog"]["status"],
        "headline": report["headline"],
        "unknown_rate": {"before": cov[BEFORE]["pooled"], "after": cov[AFTER]["pooled"]},
        "false_safe_rate": {"before": cor[BEFORE]["pooled"]["false_safe_rate"],
                            "after": cor[AFTER]["pooled"]["false_safe_rate"]},
        "report": str(md_path),
    }
