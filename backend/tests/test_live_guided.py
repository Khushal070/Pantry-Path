"""The live engine's guided-build parts, with stand-ins for the store providers and the
model, so it runs without keys, network or a model server: the background search
(prefetch) and the plan for a dish outside the catalog. Needs the live packages."""

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pantrypath_ml")

import threading  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

from app.engines.base import EngineError  # noqa: E402
from app.engines.live import LiveEngine  # noqa: E402
from pantrypath_core.result import Store  # noqa: E402
from pantrypath_pipeline.errors import LLMError  # noqa: E402

STORES = [Store(store_id=f"k{i}", chain="KROGER", name=f"Kroger {i}", address="1 Main St",
                lat=39.1, lon=-84.5, distance_miles=i) for i in (1, 2)]


class Provider:
    def __init__(self, name):
        self.name = name
        self.calls: list[tuple] = []
        self.lock = threading.Lock()

    def find_stores(self, zip_code, radius, limit):
        with self.lock:
            self.calls.append(("stores", zip_code))
        return STORES

    def search_products(self, term, store, limit):
        with self.lock:
            self.calls.append(("products", term, store.store_id))
        return []


class Cfg(dict):
    def path(self, value):
        return Path("/nonexistent") / value


def live(model_reply=None):
    engine = object.__new__(LiveEngine)
    engine.cfg = Cfg(request={"radius_miles": 10, "max_stores": 2, "max_products": 5,
                              "max_workers": 4, "parallel_providers": False,
                              "fuzzy_min_score": 90},
                     catalog={"sqlite_output": "none.sqlite"})
    engine.providers = [Provider("kroger"), Provider("target")]
    engine.store_problem = None
    engine._screen = None
    engine._prefetch_pool = ThreadPoolExecutor(2)
    engine._prefetch_lock = threading.Lock()
    engine._inflight = {}

    def complete_json(prompt, schema, *, purpose):
        assert "Dish: dal dhokli" in prompt and purpose == "plan_dish:dal dhokli"
        if isinstance(model_reply, Exception):
            raise model_reply
        return SimpleNamespace(data=model_reply)

    engine.models = SimpleNamespace(a2_client=SimpleNamespace(complete_json=complete_json))
    return engine


def test_prefetch_asks_kroger_only_and_never_twice_while_running():
    engine = live()
    kroger, target = engine.providers
    engine.prefetch("45202", [])
    engine.prefetch("45202", ["white bread", "pineapple"])
    engine.prefetch("45202", ["white bread"])               # already on its way
    engine._wait_for_prefetch("45202", ["white bread", "pineapple"])
    products = [c for c in kroger.calls if c[0] == "products"]
    assert sorted({c[1] for c in products}) == ["pineapple", "white bread"]
    assert len(products) == 4                                 # 2 terms x 2 stores, once
    assert target.calls == []                                 # Target costs credits


def test_the_final_search_waits_for_running_prefetches(monkeypatch):
    engine = live()
    seen = {}

    def fake_result(cfg, request, dish, ingredients, *, providers, catalog_version):
        seen.update(names=[i.name for i in ingredients], dish=dish, version=catalog_version,
                    inflight=[f.done() for f in engine._inflight.values()])
        raise LLMError("stop here")   # the rest is the normal search's (test_live_search)

    monkeypatch.setattr("pantrypath_pipeline.request.result_for_ingredients", fake_result)
    engine.prefetch("45202", ["white bread"])
    guided = {"dish": "sandwich", "cuisine": "american", "source": "template",
              "ingredients": [{"name": "white bread", "role": "core", "category": "bakery"},
                              {"name": "pineapple", "role": "common", "category": "other"}]}
    request = {"query": "sandwich", "zip": "45202", "avoid": [], "diet": "none"}
    with pytest.raises(EngineError):
        engine.search_ingredients(request, guided, {}, [], lambda stage: None)
    assert seen["names"] == ["white bread", "pineapple"]
    assert seen["dish"].source == "llm" and seen["version"] == "guided-template"
    assert all(seen["inflight"])


def test_a_dish_outside_the_catalog_is_planned_by_one_model_call(monkeypatch, tmp_path):
    # its own prompt: the real one (ml/prompts/plan_dish.txt) ships in ml's own PR
    prompt = tmp_path / "plan_dish.txt"
    prompt.write_text("Plan the choices.\nDish: $dish\n")
    monkeypatch.setattr("app.engines.live.PLAN_PROMPT", prompt)
    reply = {"dish": "dal dhokli", "cuisine": "south_asian", "steps": [
        {"label": "Dal", "options": [{"name": "toor dal", "category": "legume", "usual": True},
                                     {"name": "moong dal", "category": "legume",
                                      "usual": False}]},
        {"label": "Dough", "options": [{"name": "whole wheat flour", "category": "baking",
                                        "usual": True}]},
        {"label": "Garnish", "options": [{"name": "peanuts", "category": "nut_seed",
                                          "usual": True}]}]}
    plan = live(reply).plan("dal dhokli", ["peanuts"], "none")
    assert plan.source == "model" and plan.cuisine == "south_asian"
    assert plan.steps[2].left_out[0].name == "peanuts"
    usual = plan.usual()
    assert [o.name for o in usual[:2]] == ["toor dal", "whole wheat flour"]
    assert usual[2].instead_of == "peanuts"     # its swap starts ticked in its place


def test_a_failed_plan_call_is_a_plan_failed_error():
    with pytest.raises(EngineError) as err:
        live(LLMError("model down")).plan("dal dhokli", [], "none")
    assert err.value.kind == "plan_failed"


def test_a_kind_of_food_needs_no_model_call():
    plan = live(AssertionError("no model call expected")).plan("sandwich", [], "none")
    assert plan.source == "template"


def test_without_the_plan_prompt_a_dish_outside_the_catalog_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr("app.engines.live.PLAN_PROMPT", tmp_path / "missing.txt")
    engine = live(AssertionError("no model call expected"))
    with pytest.raises(EngineError) as err:
        engine.plan("dal dhokli", [], "none")
    assert err.value.kind == "plan_failed"
    assert engine.plan("sandwich", [], "none").source == "template"   # templates still work


def test_without_kroger_a_build_never_asks_target():
    engine = live()
    engine.providers = [Provider("target")]
    with pytest.raises(EngineError) as err:
        engine.options({"query": "sandwich", "zip": "45202", "avoid": [], "diet": "none"},
                       ["white bread"])
    assert err.value.kind == "config" and engine.providers[0].calls == []


def test_a_second_try_at_the_steps_reports_its_own_status_and_warnings(monkeypatch):
    from test_live_search import result_with

    engine = live()
    store = Store(store_id="1", chain="KROGER", name="Kroger", address="1 Main St",
                  lat=39.1, lon=-84.5, distance_miles=0.8)
    replies = iter([
        SimpleNamespace(status="ok", warnings=["first"], answer={
            "summary": "Quiche.", "ingredients": [], "steps": ["Stir in coconut cream."]}),
        SimpleNamespace(status="repaired", warnings=["second"], answer={
            "summary": "Quiche.", "ingredients": [], "steps": ["Beat the eggs."]}),
    ])
    engine.models = SimpleNamespace(a3_available=True, answer=lambda payload, parsed: next(replies))
    engine._vocabulary = {"coconut cream", "eggs"}
    outcome = engine._finish(result_with([store]), {"query": "quiche", "zip": "45202",
                                                    "avoid": [], "diet": "none"},
                             {}, [], lambda stage: None, {"a2": None, "stronger": False},
                             guided={"ingredients": [{"name": "eggs"}], "choices": {}})
    assert outcome.answer["steps"] == ["Beat the eggs."]
    assert outcome.answer_status == "repaired" and outcome.warnings == ["second"]
