"""Request flow: dish + ZIP -> result.json, with fake stores, catalog and OFF snapshot."""

import json
import string
from dataclasses import replace

import pytest

from pantrypath_core.catalog import Catalog, CatalogIngredient, DishProfile
from pantrypath_core.diet_rules import ProductEvidence, VerdictKind
from pantrypath_core.result import Result, Store
from pantrypath_pipeline import request as req
from pantrypath_pipeline import store as catalog_store
from pantrypath_pipeline.errors import DataCorruptError, PipelineError, ProviderError
from pantrypath_pipeline.providers.base import ProviderProduct, StoreProvider


def ingredient(name, role, category):
    return CatalogIngredient(name=name, role=role, category=category, share=0.5)


QUICHE = DishProfile(
    dish_id="quiche-lorraine", name="quiche lorraine", cuisine="european",
    ingredients=[
        ingredient("eggs", "core", "egg"),
        ingredient("heavy cream", "core", "dairy"),
        ingredient("pie crust", "core", "bakery"),
        ingredient("bacon", "common", "meat"),
        ingredient("gruyere cheese", "common", "dairy"),
    ],
    recipe_count=40, review_count=900, source="foodcom", catalog_version="2026-10-24",
)


def evidence(source, ingredients_text="", allergens=(), name=""):
    return ProductEvidence(source=source, name=name, ingredients_text=ingredients_text,
                           allergens=list(allergens))


def product(name, barcode=None, in_stock=True, ev=None, provider="kroger"):
    return ProviderProduct(provider=provider, store_id="", barcode=barcode, name=name,
                           in_stock=in_stock, evidence=ev)


KROGER_PRODUCTS = {
    "eggs": [product("Large Eggs", "0012345000010", ev=evidence("kroger", "EGGS.", ["egg"]))],
    "heavy cream": [product("Heavy Cream", "0012345000027",
                            ev=evidence("kroger", "CREAM, CARRAGEENAN.", ["milk"]))],
    "pie crust": [product("Pie Crusts", "0012345000034",
                          ev=evidence("kroger", "ENRICHED WHEAT FLOUR, PALM OIL, SALT."))],
    # Kroger has no ingredient data for the bacon: Open Food Facts must fill in
    "bacon": [product("Hickory Smoked Bacon", "0012345000058",
                      ev=evidence("kroger", name="Hickory Smoked Bacon"))],
}
TARGET_PRODUCTS = {
    "gruyere cheese": [product("Gruyere", "0012345678905", provider="target",
                               ev=evidence("target", "Pasteurized milk, salt, cultures."))],
}
OFF = {"0012345000058": evidence("off", "Pork cured with water, salt, sugar, sodium nitrite.",
                                 name="Hickory Smoked Bacon")}


def store(store_id, chain, distance):
    return Store(store_id=store_id, chain=chain, name=f"{chain.title()} {store_id}",
                 address="1 Example St, Testville, OH 45202", lat=39.1, lon=-84.5,
                 distance_miles=distance)


class FakeProvider(StoreProvider):
    def __init__(self, name, stores, products, fail_terms=()):
        super().__init__()
        self.name = name
        self.stores = stores
        self.products = products
        self.fail_terms = set(fail_terms)
        self.searched = []

    @classmethod
    def from_config(cls, cfg):
        raise NotImplementedError

    def find_stores(self, zip_code, radius_miles, limit):
        self._count_call()
        return self.stores[:limit]

    def search_products(self, term, store, limit):
        self._count_call()
        self.searched.append((term, store.store_id))
        if term in self.fail_terms or "*" in self.fail_terms:
            raise ProviderError(f"{self.name} is down")
        return [replace(p, store_id=store.store_id) for p in self.products.get(term, [])][:limit]


def kroger_fake(**kw):
    stores = kw.pop("stores", [store("k1", "KROGER", 0.7), store("k2", "KROGER", 2.5)])
    return FakeProvider("kroger", stores, kw.pop("products", KROGER_PRODUCTS), **kw)


def target_fake(**kw):
    stores = kw.pop("stores", [store("target-1", "TARGET", 1.9)])
    return FakeProvider("target", stores, kw.pop("products", TARGET_PRODUCTS), **kw)


@pytest.fixture
def flow(cfg, monkeypatch):
    """A catalog that knows quiche lorraine and an OFF snapshot that knows the bacon."""
    db = cfg.path(cfg["catalog"]["sqlite_output"])
    db.write_bytes(b"")  # only its existence matters; catalog_lookup is faked
    monkeypatch.setattr(req, "catalog_lookup",
                        lambda path, query, score: QUICHE if "quiche" in query else None)
    monkeypatch.setattr(req, "off_evidence",
                        lambda c, barcodes: {b: OFF[b] for b in barcodes if b in OFF})
    return cfg


def by_name(result):
    return {i.name: i for i in result.ingredients}


def test_quiche_for_a_vegetarian_avoiding_milk(flow):
    kroger, target = kroger_fake(), target_fake()
    result = req.build_result(flow, "quiche", "45202", ["milk"], "vegetarian",
                              providers=[kroger, target])
    items = by_name(result)
    assert [p.verdict for p in items["eggs"].products] == [VerdictKind.safe] * 2  # 2 stores
    assert items["heavy cream"].products[0].verdict == VerdictKind.unsafe
    assert items["pie crust"].products[0].verdict == VerdictKind.safe
    bacon = items["bacon"].products[0]
    assert (bacon.verdict, bacon.evidence_source) == (VerdictKind.unsafe, "off")
    assert "pork" in bacon.reason.lower()
    gruyere = items["gruyere cheese"].products[0]
    assert (gruyere.verdict, gruyere.store_id) == (VerdictKind.unsafe, "target-1")
    assert result.summary.without_safe_product == ["heavy cream", "bacon", "gruyere cheese"]
    assert result.dish.source == "catalog" and result.meta.catalog_version == "2026-10-24"
    assert result.meta.providers_used == ["kroger", "target"]
    assert [s.store_id for s in result.stores] == ["k1", "k2", "target-1"]
    Result.model_validate_json(result.model_dump_json())  # the contract holds


def test_target_only_fills_gaps(flow):
    kroger, target = kroger_fake(), target_fake()
    req.build_result(flow, "quiche", "45202", [], "none", providers=[kroger, target])
    assert len(kroger.searched) == 5 * 2  # every ingredient at both Kroger stores
    assert target.searched == [("gruyere cheese", "target-1")]


def test_out_of_stock_everywhere_triggers_the_fallback(flow):
    products = dict(KROGER_PRODUCTS)
    products["eggs"] = [replace(products["eggs"][0], in_stock=False)]
    target = target_fake()
    req.build_result(flow, "quiche", "45202", [], "none",
                     providers=[kroger_fake(products=products), target])
    assert sorted(t for t, _ in target.searched) == ["eggs", "gruyere cheese"]


def test_unknown_stock_counts_as_available(flow):
    products = dict(KROGER_PRODUCTS)
    products["eggs"] = [replace(products["eggs"][0], in_stock=None)]
    target = target_fake()
    req.build_result(flow, "quiche", "45202", [], "none",
                     providers=[kroger_fake(products=products), target])
    assert target.searched == [("gruyere cheese", "target-1")]


def test_target_takes_over_when_kroger_has_no_store(flow):
    target = target_fake(products={**TARGET_PRODUCTS, "eggs": KROGER_PRODUCTS["eggs"]})
    result = req.build_result(flow, "quiche", "45202", [], "none",
                              providers=[kroger_fake(stores=[]), target])
    assert result.meta.providers_used == ["target"]
    assert len(target.searched) == 5
    assert {s.store_id for s in result.stores} == {"target-1"}


def test_parallel_providers_asks_everyone(flow):
    flow["request"]["parallel_providers"] = True
    kroger, target = kroger_fake(), target_fake()
    req.build_result(flow, "quiche", "45202", [], "none", providers=[kroger, target])
    assert len(target.searched) == 5 and len(kroger.searched) == 10


def test_a_failed_search_is_tolerated(flow, caplog):
    result = req.build_result(flow, "quiche", "45202", [], "none",
                              providers=[kroger_fake(fail_terms={"bacon"}), target_fake()])
    assert by_name(result)["bacon"].products == []
    assert "kroger is down" in caplog.text


def test_all_searches_failing_is_an_error(flow):
    with pytest.raises(ProviderError, match="no store data could be fetched"):
        req.build_result(flow, "quiche", "45202", [], "none",
                         providers=[kroger_fake(fail_terms={"*"}), target_fake(fail_terms={"*"})])


def test_no_store_in_range_gives_an_empty_but_valid_result(flow):
    result = req.build_result(flow, "quiche", "45202", ["milk"], "none",
                              providers=[kroger_fake(stores=[]), target_fake(stores=[])])
    assert result.stores == [] and result.meta.providers_used == []
    assert result.summary.with_safe_product == 0
    assert len(result.summary.without_safe_product) == 5


def test_missing_catalog_and_no_llm_key_is_a_clear_error(cfg):
    with pytest.raises(PipelineError, match="LLM_API_KEY"):
        req.build_result(cfg, "quiche", "45202", [], "none", providers=[kroger_fake()])


def test_unknown_dish_without_llm_key_is_a_clear_error(flow):
    with pytest.raises(PipelineError, match="is not in the dish catalog"):
        req.build_result(flow, "beef wellington", "45202", [], "none",
                         providers=[kroger_fake()])


def test_llm_fallback_for_dishes_outside_the_catalog(flow, monkeypatch):
    llm_dish = QUICHE.model_copy(update={"dish_id": "khachapuri", "name": "khachapuri",
                                         "source": "llm", "recipe_count": 0})
    asked = []
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-used")
    monkeypatch.setattr(req, "llm_profile",
                        lambda c, q, client, template=None: asked.append((q, template))
                        or llm_dish)
    result = req.build_result(flow, "khachapuri", "45202", [], "none",
                              providers=[kroger_fake(), target_fake()])
    assert asked == [("khachapuri", None)]  # by default the pipeline's own prompt
    assert (result.dish.source, result.meta.catalog_version) == ("llm", "llm")


def test_llm_fallback_takes_a_prompt_template(flow, monkeypatch):
    llm_dish = QUICHE.model_copy(update={"dish_id": "khachapuri", "name": "khachapuri",
                                         "source": "llm", "recipe_count": 0})
    asked = []
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-used")
    monkeypatch.setattr(req, "llm_profile",
                        lambda c, q, client, template=None: asked.append(template) or llm_dish)
    template = (string.Template("prompt_version: other/1\n$dish_name"), "other/1")
    req.build_result(flow, "khachapuri", "45202", [], "none",
                     providers=[kroger_fake(), target_fake()], llm_template=template)
    assert asked == [template]
    asked.clear()
    req.build_result(flow, "quiche", "45202", [], "none",  # a catalog dish: no LLM call
                     providers=[kroger_fake(), target_fake()], llm_template=template)
    assert asked == []


def test_bad_zip_is_rejected(flow):
    with pytest.raises(ValueError):
        req.build_result(flow, "quiche", "4520", [], "none", providers=[kroger_fake()])


def test_catalog_lookup_reads_the_real_sqlite_catalog(tmp_path):
    db = tmp_path / "catalog.sqlite"
    catalog_store.build_sqlite(
        Catalog(catalog_version="2026-10-24", generated_at="2026-10-24T00:00:00Z",
                dishes=[QUICHE.model_copy(update={"aliases": ["bacon quiche"]})]), db)
    assert req.catalog_lookup(db, "Bacon Quiche", 90).dish_id == "quiche-lorraine"
    assert req.catalog_lookup(db, "quiche lorain", 90).dish_id == "quiche-lorraine"  # typo
    assert req.catalog_lookup(db, "pad thai", 90) is None


def test_catalog_lookup_reports_a_corrupt_catalog(tmp_path):
    db = tmp_path / "catalog.sqlite"
    db.write_text("not a database")
    with pytest.raises(DataCorruptError, match="can't be read"):
        req.catalog_lookup(db, "quiche", 90)


def test_off_evidence_without_a_snapshot_is_empty(cfg, caplog):
    assert req.off_evidence(cfg, ["0012345000058"]) == {}
    assert "snapshot" in caplog.text


def test_main_writes_result_json_and_prints_a_summary(flow, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(req, "load_config", lambda: flow)
    monkeypatch.setattr(req, "make_providers", lambda c: [kroger_fake(), target_fake()])
    out = tmp_path / "result.json"
    code = req.main(["quiche", "--zip", "45202", "--avoid", "milk", "--diet", "vegetarian",
                     "--out", str(out)])
    assert code == 0
    result = Result.model_validate_json(out.read_text())
    assert result.request.avoid == ["milk"] and result.request.diet == "vegetarian"
    printed = capsys.readouterr().out
    assert "ingredients with a safe product: 2/5" in printed
    assert "provider calls: kroger" in printed and str(out) in printed


def test_main_default_output_goes_to_results(flow, monkeypatch, capsys):
    monkeypatch.setattr(req, "load_config", lambda: flow)
    monkeypatch.setattr(req, "make_providers", lambda c: [kroger_fake()])
    assert req.main(["quiche", "--zip", "45202"]) == 0
    [written] = list(flow.path("results").glob("*_quiche-lorraine.json"))
    assert json.loads(written.read_text())["meta"]["schema_version"] == "1.0"


def test_main_reports_failures_without_a_traceback(cfg, monkeypatch, capsys):
    monkeypatch.setattr(req, "load_config", lambda: cfg)
    monkeypatch.setattr(req, "make_providers", lambda c: [kroger_fake()])
    assert req.main(["quiche", "--zip", "45202"]) == 1
    assert "Request failed" in capsys.readouterr().err


def test_main_rejects_a_bad_zip(capsys):
    with pytest.raises(SystemExit):
        req.main(["quiche", "--zip", "ABCDE"])
