"""'Not right? Ask a stronger model': the route, the chat messages it adds, the live
engine's client choice and error messages, and how the stronger model is detected from
.env. The API tests use the demo engine and a stand-in with a stronger model, so they run
without a model server, keys or network; the live tests need the live packages."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import PASSWORD, make_client, signup, wait_for

from app.engines.base import EngineError
from app.engines.demo import DemoEngine


def new_chat(client):
    return client.post("/api/sessions").json()["id"]


def say(client, session_id, text):
    response = client.post(f"/api/sessions/{session_id}/messages", json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()["session"]["messages"][-1]


def confirm(client, session_id, card):
    body = {"message_id": card["id"], **card["data"]["request"],
            "servings": card["data"]["servings"]}
    return client.post(f"/api/sessions/{session_id}/search", json=body)


class StrongerDemo(DemoEngine):
    """The demo, as if its dishes came from A2 and a stronger model were set up."""

    def __init__(self, generated: bool = True, fail_kind: str | None = None):
        super().__init__(delay_s=0)
        self.generated, self.fail_kind = generated, fail_kind
        self.calls: list[bool] = []

    def info(self):
        return {**super().info(), "stronger_model": {"available": True, "label": "Gemini"}}

    def search(self, request, extras, clarify, progress, *, stronger=False):
        self.calls.append(stronger)
        if stronger and self.fail_kind:
            raise EngineError("Gemini is busy right now. Try again in a minute.",
                              kind=self.fail_kind)
        outcome = super().search(request, extras, clarify, progress)
        if self.generated:
            outcome.result["dish"]["source"] = "llm"
        outcome.model_used = {"a2": ("Gemini" if stronger else "qwen3-4b")
                              if self.generated else None,
                              "stronger": stronger and self.generated}
        return outcome


@pytest.fixture
def stronger():
    engine = StrongerDemo()
    with make_client(engine=engine) as client:
        signup(client)
        yield client, engine


def searched(client, dish="pad thai"):
    """A chat with one finished search: (chat id, search message, its job)."""
    chat = new_chat(client)
    job_id = confirm(client, chat, say(client, chat, dish)).json()["job"]["id"]
    job = wait_for(client, job_id)
    message = client.get(f"/api/sessions/{chat}").json()["messages"][-1]
    assert message["kind"] == "search"
    return chat, message, job


# ------------------------------------------------------------ demo: off


def test_the_demo_offers_no_stronger_model(user_client):
    config = user_client.get("/api/config").json()
    assert config["stronger_model"] == {"available": False, "label": None}
    chat, message, job = searched(user_client)
    assert job["model_used"] is None
    response = user_client.post(f"/api/sessions/{chat}/messages/{message['id']}/rerun-stronger")
    assert response.status_code == 503


# ------------------------------------------------------------ with a stronger model


def test_config_names_the_stronger_model(stronger):
    client, _ = stronger
    assert client.get("/api/config").json()["stronger_model"] == {"available": True,
                                                                  "label": "Gemini"}


def test_asking_adds_a_new_search_with_the_stronger_model(stronger):
    client, engine = stronger
    chat, first, job = searched(client)
    assert job["model_used"] == {"a2": "qwen3-4b", "stronger": False}

    response = client.post(f"/api/sessions/{chat}/messages/{first['id']}/rerun-stronger")
    assert response.status_code == 200, response.text
    messages = response.json()["session"]["messages"]
    redo = messages[-1]
    assert redo["kind"] == "search" and redo["id"] != first["id"]
    assert redo["data"]["stronger"] is True and redo["data"]["redo_of"] == first["id"]
    assert redo["text"].endswith("ingredients by Gemini")
    assert redo["data"]["request"] == first["data"]["request"]
    # the first answer is untouched
    assert next(m for m in messages if m["id"] == first["id"])["data"]["job_id"] == job["id"]

    done = wait_for(client, response.json()["job"]["id"])
    assert done["status"] == "done"
    assert done["model_used"] == {"a2": "Gemini", "stronger": True}
    assert engine.calls == [False, True]


def test_rerunning_a_stronger_search_keeps_the_stronger_model(stronger):
    client, engine = stronger
    chat, first, _ = searched(client)
    redo_job = client.post(f"/api/sessions/{chat}/messages/{first['id']}/rerun-stronger"
                           ).json()["job"]["id"]
    wait_for(client, redo_job)
    redo = client.get(f"/api/sessions/{chat}").json()["messages"][-1]
    rerun = client.post(f"/api/sessions/{chat}/messages/{redo['id']}/rerun")
    assert rerun.status_code == 200
    wait_for(client, rerun.json()["job"]["id"])
    assert engine.calls == [False, True, True]


def test_a_search_can_be_redone_only_once(stronger):
    client, _ = stronger
    chat, first, _ = searched(client)
    redo_job = client.post(f"/api/sessions/{chat}/messages/{first['id']}/rerun-stronger"
                           ).json()["job"]["id"]
    wait_for(client, redo_job)
    redo = client.get(f"/api/sessions/{chat}").json()["messages"][-1]
    response = client.post(f"/api/sessions/{chat}/messages/{redo['id']}/rerun-stronger")
    assert response.status_code == 409


def test_the_same_search_is_redone_once(stronger):
    client, engine = stronger
    chat, first, _ = searched(client)
    url = f"/api/sessions/{chat}/messages/{first['id']}/rerun-stronger"
    wait_for(client, client.post(url).json()["job"]["id"])
    again = client.post(url)
    assert again.status_code == 409 and "below" in again.json()["detail"]
    assert engine.calls == [False, True]


def test_a_catalog_dish_has_nothing_to_redo():
    engine = StrongerDemo(generated=False)
    with make_client(engine=engine) as client:
        signup(client)
        chat, message, job = searched(client)
        assert job["model_used"] == {"a2": None, "stronger": False}
        response = client.post(f"/api/sessions/{chat}/messages/{message['id']}/rerun-stronger")
        assert response.status_code == 409 and "catalog" in response.json()["detail"]


def test_a_busy_stronger_model_fails_the_new_search_only():
    engine = StrongerDemo(fail_kind="stronger_busy")
    with make_client(engine=engine) as client:
        signup(client)
        chat, first, job = searched(client)
        redo_job = client.post(f"/api/sessions/{chat}/messages/{first['id']}/rerun-stronger"
                               ).json()["job"]["id"]
        failed = wait_for(client, redo_job)
        assert failed["status"] == "failed" and failed["error_kind"] == "stronger_busy"
        assert wait_for(client, job["id"])["status"] == "done"   # the first one still is


def test_someone_elses_search_is_not_found(stronger):
    client, _ = stronger
    chat, message, _ = searched(client)
    client.post("/api/auth/logout")
    signup(client, "other", prefs=None, password=PASSWORD)
    response = client.post(f"/api/sessions/{chat}/messages/{message['id']}/rerun-stronger")
    assert response.status_code == 404


def test_only_a_search_can_be_redone(stronger):
    client, _ = stronger
    chat = new_chat(client)
    card = say(client, chat, "pad thai")   # a confirm card, not a search
    response = client.post(f"/api/sessions/{chat}/messages/{card['id']}/rerun-stronger")
    assert response.status_code == 404


# ------------------------------------------------------------ live engine


def live_engine(monkeypatch, source="llm", available=True, build=None):
    """A LiveEngine without models or keys, around a stand-in build_result."""
    pytest.importorskip("pantrypath_ml")
    from app.engines.live import LiveEngine
    from app.stronger_model import StrongerModel
    from pantrypath_core.result import DishInfo, IngredientResult, Meta, Result, Store, Summary

    calls = []

    def fake_build(*args, **kwargs):
        calls.append(kwargs)
        if build is not None:
            build()
        ingredients = [IngredientResult(name="toor dal", role="core", category="legume",
                                        products=[])]
        now = datetime.now(UTC)
        return Result(request={"query": "dal dhokli", "zip": "45202", "avoid": [],
                               "diet": "none"},
                      dish=DishInfo(name="dal dhokli", cuisine="south_asian", source=source),
                      stores=[Store(store_id="1", chain="KROGER", name="Kroger",
                                    address="1 Main St", lat=39.1, lon=-84.5,
                                    distance_miles=0.6)],
                      ingredients=ingredients, summary=Summary.from_ingredients(ingredients),
                      meta=Meta(generated_at=now, expires_at=now + timedelta(hours=1),
                                catalog_version="test"))

    live = object.__new__(LiveEngine)
    live.cfg = {"request": {"radius_miles": 10}}
    live.providers = [SimpleNamespace(name="kroger")]
    live.store_problem = None
    live.models = SimpleNamespace(a2_client="QWEN", a2_template="A2-PROMPT",
                                  a3_available=False, setting={"candidate": "qwen3-4b"})
    live.stronger = (StrongerModel(True, label="Gemini", provider="gemini", model="m")
                     if available else StrongerModel(False, reason="LLM_API_KEY is empty"))
    monkeypatch.setattr("pantrypath_pipeline.request.build_result", fake_build)
    return live, calls


REQUEST = {"query": "dal dhokli", "zip": "45202", "avoid": [], "diet": "none"}


def test_live_uses_the_champion_unless_asked(monkeypatch):
    live, calls = live_engine(monkeypatch)
    outcome = live.search(REQUEST, {}, [], lambda stage: None)
    assert calls[-1]["llm_client"] == "QWEN" and calls[-1]["llm_template"] == "A2-PROMPT"
    assert outcome.model_used == {"a2": "qwen3-4b", "stronger": False}


def test_live_stronger_uses_the_env_client_with_the_same_prompt(monkeypatch):
    live, calls = live_engine(monkeypatch)
    outcome = live.search(REQUEST, {}, [], lambda stage: None, stronger=True)
    assert calls[-1]["llm_client"] is None and calls[-1]["llm_template"] == "A2-PROMPT"
    assert outcome.model_used == {"a2": "Gemini", "stronger": True}


def test_live_catalog_dish_names_no_model(monkeypatch):
    live, _ = live_engine(monkeypatch, source="catalog")
    outcome = live.search(REQUEST, {}, [], lambda stage: None, stronger=True)
    assert outcome.model_used == {"a2": None, "stronger": False}


def test_live_stronger_without_a_key_says_so(monkeypatch):
    live, calls = live_engine(monkeypatch, available=False)
    with pytest.raises(EngineError) as err:
        live.search(REQUEST, {}, [], lambda stage: None, stronger=True)
    assert err.value.kind == "stronger_unavailable" and calls == []


@pytest.mark.parametrize(("message", "kind"), [
    ("LLM request still failing after 4 attempts: HTTP 429: quota", "stronger_quota"),
    ("LLM request still failing after 4 attempts: HTTP 503: high demand", "stronger_busy"),
    ("LLM request failed (HTTP 404): no longer available to new users", "stronger_failed"),
    ("the reply was not JSON", "stronger_failed"),
])
def test_live_stronger_errors_say_what_to_do(monkeypatch, message, kind):
    pytest.importorskip("pantrypath_ml")
    from pantrypath_pipeline.errors import LLMError

    def fail():
        raise LLMError(f"Could not get ingredients for 'dal dhokli' from the LLM: {message}")

    live, _ = live_engine(monkeypatch, build=fail)
    with pytest.raises(EngineError) as err:
        live.search(REQUEST, {}, [], lambda stage: None, stronger=True)
    assert err.value.kind == kind and "Gemini" in str(err.value)


def test_champion_failure_keeps_its_old_message(monkeypatch):
    pytest.importorskip("pantrypath_ml")
    from pantrypath_pipeline.errors import LLMError

    def fail():
        raise LLMError("HTTP 503")

    live, _ = live_engine(monkeypatch, build=fail)
    with pytest.raises(EngineError) as err:
        live.search(REQUEST, {}, [], lambda stage: None)
    assert err.value.kind == "not_found"


# ------------------------------------------------------------ detection from .env


@pytest.fixture
def env(monkeypatch):
    pytest.importorskip("pantrypath_pipeline")
    for name in ("LLM_API_KEY", "LLM_PROVIDER", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


CFG = {"llm": {"provider": "auto", "models": {"gemini": "gemini-default"}}}


def test_no_key_means_no_stronger_model(env):
    from app.stronger_model import detect

    found = detect(CFG)
    assert not found.available and "LLM_API_KEY" in found.reason
    assert found.view() == {"available": False, "label": None}


def test_a_gemini_key_and_model_from_env(env):
    from app.stronger_model import detect

    env.setenv("LLM_API_KEY", "test-key")
    env.setenv("LLM_PROVIDER", "gemini")
    env.setenv("LLM_MODEL", "gemini-3.6-flash")
    found = detect(CFG)
    assert found.available and found.label == "Gemini" and found.model == "gemini-3.6-flash"


def test_the_model_falls_back_to_the_pipeline_default(env):
    from app.stronger_model import detect

    env.setenv("LLM_API_KEY", "AIza-test")   # a Gemini key, provider guessed
    found = detect(CFG)
    assert found.available and found.provider == "gemini" and found.model == "gemini-default"


def test_replay_and_unknown_providers_are_off(env):
    from app.stronger_model import detect

    env.setenv("LLM_API_KEY", "test-key")
    env.setenv("LLM_PROVIDER", "replay")
    assert not detect(CFG).available
    env.setenv("LLM_PROVIDER", "nonsense")
    assert not detect(CFG).available
