"""Bias report: slicing helpers, ground truth, Fairlearn metrics, and the written report.

Most tests use a stand-in for diet_rules.assess (fake_assess) on the 10 products in
fixtures/bias_products.json, so every expected metric can be worked out by hand.
"""

import json
import math
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from pantrypath_core import diet_rules
from pantrypath_core.catalog import Catalog, CatalogIngredient, DishProfile
from pantrypath_core.diet_rules import Allergen, Diet, ProductEvidence, Verdict
from pantrypath_pipeline import bias
from pantrypath_pipeline.errors import DataUnavailableError

SCHEMA = pa.schema([
    ("barcode", pa.string()), ("code", pa.string()), ("product_name", pa.string()),
    ("brands", pa.string()), ("categories_tags", pa.list_(pa.string())),
    ("allergens_tags", pa.list_(pa.string())), ("traces_tags", pa.list_(pa.string())),
    ("ingredients_text", pa.string()), ("labels_tags", pa.list_(pa.string())),
    ("unique_scans_n", pa.int32()),
])


@pytest.fixture
def products(fixtures_dir):
    return json.loads((fixtures_dir / "bias_products.json").read_text())


@pytest.fixture
def by_code(products):
    return {p["barcode"][-2:]: p for p in products}


@pytest.fixture
def snapshot_cfg(cfg, products):
    """Fixture products as the OFF snapshot; no sampling; every group counts for gaps."""
    path = cfg.path(cfg["off"]["output"])
    rows = [{"code": p["barcode"], "traces_tags": [], **p} for p in products]
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), path)
    cfg.data["bias"].update(coverage_sample_size=0, labelled_sample_size=0, min_slice_size=1)
    return cfg


def fake_assess(ev: ProductEvidence, avoid, diet: Diet) -> Verdict:
    """Deterministic stand-in for diet_rules.assess (keyword checks only)."""
    words = " ".join([ev.ingredients_text, ev.name, *ev.categories, *ev.allergens]).lower()

    def verdict(kind: str) -> Verdict:
        return Verdict(verdict=kind, reason="fake", evidence_source="off")

    if not ev.ingredients_text and not ev.allergens:
        return verdict("unknown")
    if Allergen.gluten in set(avoid) and ("gluten" in words or "wheat" in words):
        return verdict("unsafe")
    meat = any(w in words for w in ("beef", "bacon", "meats"))
    if diet in (Diet.vegan, Diet.vegetarian) and meat:
        return verdict("unsafe")
    if diet == Diet.vegan and any(w in words for w in ("milk", "cheese", "dairies")):
        return verdict("unsafe")
    return verdict("safe" if ev.ingredients_text else "unknown")


# ---------------------------------------------------------------- slicing helpers


def test_ground_truth_uses_claims_facts_and_drops_conflicts(by_code):
    assert bias.ground_truth(by_code["01"]) == {
        "vegan": "unsafe", "vegetarian": "unsafe", "gluten_free": None}
    assert bias.ground_truth(by_code["03"]) == {
        "vegan": "safe", "vegetarian": "safe", "gluten_free": "safe"}
    assert bias.ground_truth(by_code["05"]) == {
        "vegan": None, "vegetarian": "safe", "gluten_free": "unsafe"}
    # Vegan-labelled cheese alternative filed under en:cheeses: a conflict for vegans only
    assert bias.ground_truth(by_code["07"]) == {
        "vegan": "conflict", "vegetarian": "safe", "gluten_free": None}
    assert bias.ground_truth(by_code["10"])["vegan"] == "unsafe"  # honey


@pytest.mark.parametrize(("row", "profile"), [
    ({"product_name": "Veggie Burger", "categories_tags": ["en:meats"]}, "vegetarian"),
    ({"product_name": "Strawberry Cashew Milk", "categories_tags": ["en:dairies"]}, "vegan"),
    ({"product_name": "Plantbased Frichik", "categories_tags": ["en:poultries"]}, "vegetarian"),
    ({"product_name": "Mediterranean Patties", "brands": "Veggie Patch",
      "categories_tags": ["en:meats"]}, "vegetarian"),
    ({"product_name": "Gluten-Free Chicken Nuggets", "allergens_tags": ["en:gluten"]},
     "gluten_free"),
])
def test_name_contradicting_a_fact_is_a_conflict(row, profile):
    """Open Food Facts files some meat-free products under meats; don't count them unsafe."""
    assert bias.ground_truth(row)[profile] == "conflict"


def test_plant_based_versions_are_never_animal_facts():
    row = {"categories_tags": ["en:sausages", "en:vegetarian-sausages"], "labels_tags": []}
    assert bias.ground_truth(row)["vegetarian"] is None
    assert bias.category_group(row["categories_tags"]) == "plant_based_substitutes"


@pytest.mark.parametrize(("tags", "group"), [
    (["en:meals", "en:meats"], "meat_seafood"),
    (["en:frozen-desserts", "en:ice-creams"], "dairy_eggs"),
    (["en:sweeteners", "en:honeys"], "pantry_condiments"),
    (["en:dietary-supplements"], "other"),
    (["en:undefined"], "uncategorized"),
    ([], "uncategorized"),
    (None, "uncategorized"),
])
def test_category_group(tags, group):
    assert bias.category_group(tags) == group


@pytest.mark.parametrize(("brands", "kind"), [
    ("Kroger, The Kroger Co.", "store_brand"),
    ("Trader Joe’s", "store_brand"),
    ("Wegmans Food Markets", "store_brand"),
    ("Green Giant", "national_brand"),
    ("Heinz", "national_brand"),
    ("", "no_brand"),
    (None, "no_brand"),
])
def test_brand_type(brands, kind):
    stores = frozenset(bias.normalize_brand(b) for b in ["kroger", "trader joe's", "wegmans",
                                                         "giant", 365])
    assert bias.brand_type(brands, stores) == kind


def test_popularity_tiers():
    bins = [0, 1, 10]
    assert bias.tier_labels(bins) == ["0 scans", "1-9 scans", "10+ scans"]
    tiers = [bias.popularity_tier(n, bins) for n in (None, 0, 1, 9, 10, 500)]
    assert tiers == ["0 scans", "0 scans", "1-9 scans", "1-9 scans", "10+ scans", "10+ scans"]


# ---------------------------------------------------------------- metrics


def test_rate_metrics_on_known_arrays():
    truth = ["safe", "safe", "unsafe", "unsafe", "unsafe"]
    verdicts = ["safe", "unknown", "safe", "unsafe", "unknown"]
    assert bias.unknown_rate(truth, verdicts) == pytest.approx(2 / 5)
    assert bias.accuracy_decided(truth, verdicts) == pytest.approx(2 / 3)
    assert bias.false_safe_rate(truth, verdicts) == pytest.approx(1 / 3)
    assert bias.safe_found_rate(truth, verdicts) == pytest.approx(1 / 2)
    # Nothing to measure -> NaN (shown as n/a), not zero
    assert math.isnan(bias.false_safe_rate(["safe"], ["safe"]))
    assert math.isnan(bias.accuracy_decided(["safe"], ["unknown"]))
    assert math.isnan(bias.unknown_rate([], []))


def test_slice_metrics_by_group_and_gaps_skip_small_groups():
    frame = pd.DataFrame({
        "truth": ["unsafe"] * 4 + ["unsafe"] * 4 + ["unsafe"],
        "verdict": ["safe", "unsafe", "unsafe", "unsafe"] + ["unknown"] * 4 + ["safe"],
        "brand_type": ["store_brand"] * 4 + ["national_brand"] * 4 + ["no_brand"],
    })
    out = bias.slice_metrics(frame, "brand_type", min_size=2)
    assert out["by_group"]["store_brand"]["n"] == 4
    assert out["by_group"]["store_brand"]["false_safe_rate"] == pytest.approx(0.25)
    assert out["by_group"]["national_brand"]["unknown_rate"] == 1.0
    # no_brand (1 verdict, 100% false safe) is listed but too small to define a gap
    assert out["by_group"]["no_brand"]["false_safe_rate"] == 1.0
    gap = out["gaps"]["false_safe_rate"]
    assert gap["difference"] == pytest.approx(0.25)
    assert (gap["max_group"], gap["min_group"]) == ("store_brand", "national_brand")
    assert out["gaps"]["unknown_rate"]["difference"] == pytest.approx(1.0)
    # accuracy needs 2+ decided verdicts per group: only store_brand has them -> no gap
    assert "accuracy_decided" not in out["gaps"]


def test_jsonable_turns_nan_into_null():
    data = {"a": np.float64("nan"), "b": [np.int64(3), 0.123456], "c": float("inf")}
    assert bias._jsonable(data) == {"a": None, "b": [3, 0.1235], "c": None}


# ---------------------------------------------------------------- products on the fixture


def test_products_section_metrics_on_fixture(snapshot_cfg):
    p = bias._jsonable(bias.products_section(
        snapshot_cfg, bias._off_evidence_fn(), fake_assess))
    assert (p["coverage_products"], p["labelled_products"]) == (10, 9)
    assert p["truth_counts"] == {
        "vegan": {"safe": 3, "unsafe": 4, "conflict_excluded": 1},
        "vegetarian": {"safe": 5, "unsafe": 2, "conflict_excluded": 0},
        "gluten_free": {"safe": 2, "unsafe": 1, "conflict_excluded": 0},
    }
    cov = p["overall"]["coverage"]
    assert cov["tags_only"]["pooled"] == pytest.approx(28 / 30, abs=1e-4)
    assert cov["tags_only"]["vegan"] == pytest.approx(0.9)
    assert cov["full"]["pooled"] == pytest.approx(0.1)

    cor = p["overall"]["correctness"]
    before, text, after = (cor[m]["pooled"] for m in ("tags_only", "tags_and_text", "full"))
    assert before["n"] == 17
    assert (before["unknown_rate"], before["accuracy_decided"]) == (pytest.approx(15 / 17,
                                                                                abs=1e-4), 1.0)
    assert (before["false_safe_rate"], before["safe_found_rate"]) == (0.0, 0.0)
    # Ingredient text alone misses the cure-only bacon (and honey): 3 of 7 unsafe marked safe
    assert text["false_safe_rate"] == pytest.approx(3 / 7, abs=1e-4)
    assert text["accuracy_decided"] == pytest.approx(14 / 17, abs=1e-4)
    # The name check catches the bacon; honey stays wrong (the fake rules don't know it)
    assert after["false_safe_rate"] == pytest.approx(1 / 7, abs=1e-4)
    assert after["accuracy_decided"] == pytest.approx(16 / 17, abs=1e-4)
    assert after["safe_found_rate"] == 1.0

    brands = p["slices"]["brand_type"]["correctness"]["full"]["by_group"]
    assert brands["store_brand"]["n"] == 7 and brands["store_brand"]["n_unsafe"] == 4
    assert brands["store_brand"]["false_safe_rate"] == 0.25
    assert brands["national_brand"]["false_safe_rate"] == 0.0
    gap = p["slices"]["brand_type"]["coverage"]["full"]["gaps"]["unknown_rate"]
    assert gap["difference"] == 1.0 and gap["max_group"] == "no_brand"
    groups = p["slices"]["category_group"]["coverage"]["full"]["by_group"]
    assert groups["meat_seafood"]["n"] == 6  # 2 products x 3 users
    assert set(groups) == {"meat_seafood", "plant_based_substitutes", "dairy_eggs",
                           "bakery_cereals", "snacks_sweets", "pantry_condiments",
                           "uncategorized"}
    tiers = p["slices"]["popularity_tier"]["coverage"]["full"]["by_group"]
    assert {t: m["n"] // 3 for t, m in tiers.items()} == {
        "0 scans": 3, "1-9 scans": 3, "10+ scans": 4}


def test_run_writes_json_and_markdown(snapshot_cfg):
    summary = bias.run(snapshot_cfg, assess_fn=fake_assess)
    report = json.loads(snapshot_cfg.path(snapshot_cfg["bias"]["output_json"]).read_text())
    markdown = snapshot_cfg.path(snapshot_cfg["bias"]["output_md"]).read_text()

    assert summary["unknown_rate"] == {"before": pytest.approx(0.9333), "after": 0.1}
    assert summary["catalog"] == "pending"
    assert report["headline"] == summary["headline"]
    assert "from 93% to 10%" in report["headline"]
    # Ties go to the first slicing: category group (uncategorized has no evidence at all)
    assert "by category group: uncategorized 100% vs" in report["headline"]
    assert any("14.3% with the full rules" in f for f in report["findings"])
    for heading in ("## Findings", "## 1. Dish catalog by cuisine", "### Overall",
                    "### Largest gaps between slices", "## 3. Mitigation and trade-offs",
                    "## 4. Limitations"):
        assert heading in markdown
    assert "_Pending: the catalog needs an LLM run" in markdown
    assert "| all users | 93% → 10% → 10% |" in markdown
    # The honey the fake rules miss is listed for whoever improves diet_rules
    assert report["products"]["false_safe_examples"] == {
        "vegan": [{"barcode": "0000000000010", "name": "Clover Honey", "brand": "Great Value",
                   "category_group": "pantry_condiments"}],
        "vegetarian": [], "gluten_free": []}
    assert "| vegan | Clover Honey | Great Value | pantry condiments |" in markdown
    assert report["products"]["false_unsafe_examples"] == {
        "vegan": [], "vegetarian": [], "gluten_free": []}
    assert "labelled safe, rules say unsafe" not in markdown  # no rows, no table
    assert "**Not a held-out evaluation.**" in markdown and "**Label noise.**" in markdown
    assert any("in uncategorized the vegan user is affected most (100% unknown)" in f
               for f in report["findings"])


def test_missing_snapshot_is_reported(cfg):
    with pytest.raises(DataUnavailableError, match="filter-off"):
        bias.run(cfg, assess_fn=fake_assess)


def _write_snapshot(cfg, products):
    rows = [{"code": p["barcode"], "traces_tags": [], **p} for p in products]
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), cfg.path(cfg["off"]["output"]))


def test_no_seen_list_means_no_held_out_check(snapshot_cfg):
    bias.run(snapshot_cfg, assess_fn=fake_assess)
    report = json.loads(snapshot_cfg.path(snapshot_cfg["bias"]["output_json"]).read_text())
    assert report["products"]["held_out"]["status"] == "no_seen_list"
    assert report["products"]["held_out"]["products"] == 0
    markdown = snapshot_cfg.path(snapshot_cfg["bias"]["output_md"]).read_text()
    assert "No list of seen products" in markdown


def test_frozen_snapshot_has_no_held_out_products(snapshot_cfg):
    frozen = bias.run_freeze_seen(snapshot_cfg)
    assert frozen["seen_products"] == 10
    summary = bias.run(snapshot_cfg, assess_fn=fake_assess)
    assert summary["held_out_products"] == 0
    markdown = snapshot_cfg.path(snapshot_cfg["bias"]["output_md"]).read_text()
    assert "**0 held-out products in this snapshot**" in markdown


def test_held_out_scores_only_products_added_after_the_freeze(snapshot_cfg, products):
    _write_snapshot(snapshot_cfg, products[:6])
    bias.run_freeze_seen(snapshot_cfg)
    _write_snapshot(snapshot_cfg, products)  # a later snapshot adds 4 products
    path = snapshot_cfg.path(snapshot_cfg["off"]["output"])
    new = {p["barcode"] for p in products[6:]}
    labelled_new = [r for r in bias.load_rows(path, where=bias.LABELLED_FILTER)
                    if r["barcode"] in new]

    p = bias._jsonable(bias.products_section(snapshot_cfg, bias._off_evidence_fn(),
                                             fake_assess))
    h = p["held_out"]
    assert h["status"] == "ok" and h["seen_products"] == 6
    assert h["products"] == len(labelled_new) > 0
    expected = bias._truth_counts(labelled_new)
    assert h["truth_counts"] == expected
    verdicts = sum(c["safe"] + c["unsafe"] for c in expected.values())
    assert h["correctness"]["full"]["pooled"]["n"] == verdicts
    # the fixed-sample numbers still use every product
    assert p["labelled_products"] == 9


def test_freezing_again_adds_only_new_barcodes(snapshot_cfg, products):
    _write_snapshot(snapshot_cfg, products[:6])
    assert bias.run_freeze_seen(snapshot_cfg)["seen_products"] == 6
    _write_snapshot(snapshot_cfg, products)
    assert bias.run_freeze_seen(snapshot_cfg)["seen_products"] == 10
    assert bias.run_freeze_seen(snapshot_cfg)["seen_products"] == 10  # no duplicates


def test_held_out_markdown_flags_small_sets():
    pooled = {"n": 3, "unknown_rate": 0.0, "accuracy_decided": 1.0, "false_safe_rate": 0.0,
              "safe_found_rate": 1.0}
    h = {"status": "ok", "frozen_on": "2026-10-26", "seen_products": 100, "products": 2,
         "correctness": {m: {p: pooled for p in [*bias.PROFILES, "pooled"]}
                         for m in (bias.BEFORE, bias.AFTER)}}
    text = "\n".join(bias._held_out_md(h, min_size=30))
    assert "**2 held-out products** with a ground truth (fewer than 30" in text
    assert "| all users | 3 | 0% → 0% | 100% → 100% | 0.0% → 0.0% | 100% → 100% |" in text


def test_sample_is_the_same_every_run(snapshot_cfg):
    path = snapshot_cfg.path(snapshot_cfg["off"]["output"])
    first = [r["barcode"] for r in bias.load_rows(path, limit=4, seed=7)]
    assert first == [r["barcode"] for r in bias.load_rows(path, limit=4, seed=7)]
    assert len(first) == 4


# ---------------------------------------------------------------- catalog


def _dish(dish_id, cuisine, n_ingredients, n_llm, recipes, reviews):
    ingredients = [
        CatalogIngredient(name=f"ingredient {i}", role="core" if i == 0 else "common",
                          category="other", share=0.0 if i < n_llm else 0.5,
                          added_by_llm=i < n_llm)
        for i in range(n_ingredients)
    ]
    return DishProfile(dish_id=dish_id, name=dish_id.replace("-", " "), cuisine=cuisine,
                       ingredients=ingredients, recipe_count=recipes, review_count=reviews,
                       source="foodcom", catalog_version="test")


def test_catalog_section_compares_cuisines(cfg):
    pool = {"plain_top100_cuisine_hints": {"american": 1, "european": 13, "latin_american": 7,
                                           "east_se_asian": 4, "south_asian": 1,
                                           "middle_eastern_african": 1, "unknown": 73}}
    cfg.path(cfg["foodcom"]["pool_output"]).write_text(json.dumps(pool))
    catalog = Catalog(catalog_version="test", generated_at=datetime.now(UTC), dishes=[
        _dish("meatloaf", "american", 4, 0, 100, 1000),
        _dish("pot-roast", "american", 4, 0, 50, 500),
        _dish("quiche-lorraine", "european", 5, 1, 10, 80),
        _dish("chana-masala", "south_asian", 3, 1, 6, 20),
    ])
    cfg.path(cfg["catalog"]["profiles_output"]).write_text(catalog.model_dump_json())

    c = bias._jsonable(bias.catalog_section(cfg))
    assert c["status"] == "ok" and c["after"] == {"american": 2, "european": 1, "south_asian": 1}
    assert c["before"]["unknown"] == 73
    american, european = c["per_cuisine"]["american"], c["per_cuisine"]["european"]
    assert (american["dishes"], american["ingredients_per_dish"]) == (2, 4.0)
    assert (american["llm_added_share"], american["median_recipes"]) == (0.0, 75.0)
    assert european["llm_added_share"] == 0.2
    assert c["per_cuisine"]["south_asian"]["llm_added_share"] == pytest.approx(0.3333)
    assert c["gaps"]["ingredients_per_dish"]["difference"] == 2.0
    other = c["american_vs_other"]["other cuisines"]
    assert (other["median_recipes"], other["llm_added_share"]) == (8.0, pytest.approx(0.2667))

    findings = bias.catalog_findings(c)
    assert findings[0].startswith("Catalog before mitigation: a plain top 100 by popularity "
                                  "is 74% American or untagged")
    assert "4 dishes, 50% American (target 60%)" in findings[1]
    assert "Below quota: american 2/60" in findings[2]
    note = bias.mitigation_notes({"catalog": c, "products": _empty_products()})[0]
    assert "down from 74% American or untagged" in note
    assert "backed by fewer source recipes (median 8 vs 75 for American dishes)" in note
    assert "with more LLM-added ingredients (27% vs 0%), tagged `added_by_llm`" in note


def _empty_products():
    pooled = {"false_safe_rate": None, "safe_found_rate": None, "accuracy_decided": None}
    return {"overall": {
        "coverage": {m: {"pooled": None} for m in bias.MODES},
        "correctness": {m: {"pooled": pooled} for m in bias.MODES},
    }}


def test_catalog_pending_without_profiles(cfg):
    c = bias.catalog_section(cfg)
    assert c["status"] == "pending" and c["before"] is None
    assert bias.catalog_findings(c) == [
        "Catalog after mitigation: pending; the catalog needs an LLM run (refine-all, then "
        "assemble-catalog)."]


# ---------------------------------------------------------------- the real rules


def test_real_diet_rules_run_on_the_fixture(snapshot_cfg):
    probe = ProductEvidence(source="off", ingredients_text="water")
    try:
        diet_rules.assess(probe, [], Diet.vegan)
    except NotImplementedError:
        pytest.skip("diet_rules is not implemented yet")
    p = bias.products_section(snapshot_cfg, bias._off_evidence_fn(), diet_rules.assess)
    for mode in bias.MODES:
        for metrics in p["slices"]["category_group"]["coverage"][mode]["by_group"].values():
            assert 0 <= metrics["unknown_rate"] <= 1
    # Cure-only bacon: the ingredient text lists no meat, the name does
    row = {"barcode": "x", "product_name": "Hickory Smoked Bacon",
           "categories_tags": ["en:meats"], "ingredients_text": "water, salt, sodium nitrite"}
    evidence = bias._off_evidence_fn()(row)
    assert diet_rules.assess(evidence, [], Diet.vegetarian).verdict == "unsafe"
