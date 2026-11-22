"""LiveEngine.search around the request flow, with stand-ins for build_result and A3, so
it runs without a model server, store keys or network. Needs the live packages."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("pantrypath_ml")

from app.engines.base import EngineError  # noqa: E402
from app.engines.live import LiveEngine, no_stores_message  # noqa: E402
from pantrypath_core.result import (  # noqa: E402
    DishInfo,
    IngredientResult,
    Meta,
    Result,
    Store,
    Summary,
)

REQUEST = {"query": "quiche", "zip": "10010", "avoid": ["milk"], "diet": "none"}


def result_with(stores):
    ingredients = [IngredientResult(name="eggs", role="core", category="egg", products=[])]
    now = datetime.now(UTC)
    return Result(request=REQUEST, dish=DishInfo(name="quiche lorraine", cuisine="european",
                                                 source="catalog"),
                  stores=stores, ingredients=ingredients,
                  summary=Summary.from_ingredients(ingredients),
                  meta=Meta(generated_at=now, expires_at=now + timedelta(hours=1),
                            catalog_version="test"))


def engine(monkeypatch, result, providers=("kroger",)):
    def no_a3(*args, **kwargs):
        raise AssertionError("A3 must not run without stores")

    live = object.__new__(LiveEngine)   # no models or keys: only what search() reads
    live.cfg = {"request": {"radius_miles": 10}}
    live.providers = [SimpleNamespace(name=name) for name in providers]
    live.store_problem = None
    live.models = SimpleNamespace(a2_client=None, a2_template=None, a3_available=True,
                                  answer=no_a3)
    monkeypatch.setattr("pantrypath_pipeline.request.build_result",
                        lambda *args, **kwargs: result)
    return live


def test_no_store_near_the_zip_says_so_and_skips_a3(monkeypatch):
    live = engine(monkeypatch, result_with([]))
    with pytest.raises(EngineError) as err:
        live.search(REQUEST, {}, [], lambda stage: None)
    assert err.value.kind == "no_stores"
    message = str(err.value)
    assert "Kroger-family store within 10 miles of 10010" in message
    assert "45202" in message and "RedCircle" in message


def test_the_message_names_target_when_it_is_searched_too():
    message = no_stores_message("10010", 10, ["kroger", "target"])
    assert "Kroger-family or Target store" in message and "RedCircle" not in message


def test_a_result_with_stores_goes_on_to_a3(monkeypatch):
    store = Store(store_id="1", chain="KROGER", name="Kroger", address="1 Main St",
                  lat=39.1, lon=-84.5, distance_miles=0.8)
    live = engine(monkeypatch, result_with([store]))
    reply = SimpleNamespace(answer={"summary": "ok"}, status="ok", warnings=["w"])
    live.models.answer = lambda payload, parsed: reply
    outcome = live.search(REQUEST, {}, [], lambda stage: None)
    assert outcome.answer == {"summary": "ok"} and outcome.warnings == ["w"]
