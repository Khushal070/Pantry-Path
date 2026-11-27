"""Guided builds through the API, with the demo engine: name a dish, choose step by step,
add your own items, and search one merged list."""

import pytest
from conftest import make_client, saved_text, signup, wait_for

from app.engines.demo import DemoEngine

PREFS = {"name": "Ana", "zip": "45202", "avoid": ["peanuts"], "diet": "vegetarian",
         "servings": 2, "needs": []}


class Recording(DemoEngine):
    """The demo engine, remembering what it was asked to fetch in the background."""

    def __init__(self) -> None:
        super().__init__(delay_s=0)
        self.prefetched: list[tuple[str, list[str]]] = []

    def prefetch(self, zip_code: str, terms: list[str]) -> None:
        self.prefetched.append((zip_code, list(terms)))


@pytest.fixture
def engine():
    return Recording()


@pytest.fixture
def client(engine):
    with make_client(engine=engine, guided_builds=True) as c:
        signup(c, "ana", prefs=PREFS)
        yield c


def say(client, chat, text):
    response = client.post(f"/api/sessions/{chat}/messages", json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()["session"]


def act(client, chat, card_id, expect=200, **body):
    response = client.post(f"/api/sessions/{chat}/builds/{card_id}", json=body)
    assert response.status_code == expect, response.text
    return response.json()


def start(client, text="sandwich"):
    chat = client.post("/api/sessions").json()["id"]
    card = say(client, chat, text)["messages"][-1]
    assert card["kind"] == "build", card
    return chat, card


def current_step(card):
    return card["data"]["plan"]["steps"][card["data"]["step"]]


def test_a_dish_starts_a_build_with_the_usual_ticked_and_clashes_left_out(client, engine):
    chat, card = start(client)
    build = card["data"]
    assert card["text"] == "Choosing what goes in your sandwich"
    assert build["plan"]["source"] == "template" and build["step"] == 0
    assert build["picks"]["s1"] == ["white bread"]
    protein = build["plan"]["steps"][1]
    assert "ham" in [x["name"] for x in protein["left_out"]]        # vegetarian
    assert engine.prefetched[0] == ("45202", [])                     # stores first
    assert "white bread" in engine.prefetched[1][1]                  # then the usual options


def test_choosing_step_by_step_ends_in_a_summary_and_one_merged_search(client, engine):
    chat, card = start(client)
    act(client, chat, card["id"], action="next", picks=["sourdough bread"])
    assert ("45202", ["sourdough bread"]) in engine.prefetched       # each new pick
    session = act(client, chat, card["id"], action="add", text="add pineapple")["session"]
    assert session["messages"][-1]["id"] == card["id"]
    for _ in range(len(card["data"]["plan"]["steps"])):
        last = session["messages"][-1]
        if last["kind"] != "build":
            break
        session = act(client, chat, card["id"], action="next")["session"]
    summary = session["messages"][-1]
    assert summary["kind"] == "confirm"
    names = [i["name"] for i in summary["data"]["guided"]["ingredients"]]
    assert names[0] == "sourdough bread" and names[-1] == "pineapple"
    assert summary["data"]["guided"]["added"] == ["pineapple"]
    body = {"message_id": summary["id"], **summary["data"]["request"], "servings": 2}
    reply = client.post(f"/api/sessions/{chat}/search", json=body).json()
    job = wait_for(client, reply["job"]["id"])
    assert job["status"] == "done"
    assert [i["name"] for i in job["result"]["ingredients"]] == names
    assert job["result"]["request"]["avoid"] == ["peanuts"]
    assert not (job["model_used"] or {}).get("a2")   # no "Ask a stronger model"
    search = reply["session"]["messages"][-1]
    assert client.post(f"/api/sessions/{chat}/messages/{search['id']}/rerun-stronger") \
        .status_code in (409, 503)


def test_typing_during_a_build_adds_items_and_a_clash_waits_for_a_choice(client):
    chat, card = start(client)
    session = say(client, chat, "add pineapple and peanut butter, no tomatoes")
    build = session["messages"][-1]
    assert build["id"] == card["id"] and session["messages"][-2]["text"].startswith("add")
    items = {a["name"]: a for a in build["data"]["additions"]}
    assert items["pineapple"]["status"] == "ok"
    clash = items["peanut butter"]["clash"]
    assert items["peanut butter"]["status"] == "clash" and clash["swap"]
    assert "tomatoes" not in build["data"]["picks"]["s4"]
    blocked = act(client, chat, card["id"], expect=409, action="finish")
    assert "peanut butter" in blocked["detail"]
    act(client, chat, card["id"], action="resolve", name="peanut butter", choice="swap")
    summary = act(client, chat, card["id"], action="finish")["session"]["messages"][-1]
    added = summary["data"]["guided"]["added"]
    assert added == ["pineapple", clash["swap"]]


def test_search_anyway_keeps_the_item_and_its_products_are_judged(client):
    chat, card = start(client)
    act(client, chat, card["id"], action="add", text="peanut butter")
    act(client, chat, card["id"], action="resolve", name="peanut butter", choice="anyway")
    reply = act(client, chat, card["id"], action="usual")
    job = wait_for(client, reply["job"]["id"])
    pb = next(i for i in job["result"]["ingredients"] if i["name"] == "peanut butter")
    assert pb["products"] and all(p["verdict"] != "safe" or "peanut" not in p["name"].lower()
                                  for p in pb["products"])


def test_use_the_usual_on_an_untouched_catalog_dish_is_todays_search(client):
    chat, card = start(client, "quiche lorraine")
    assert card["data"]["plan"]["source"] == "catalog"
    reply = act(client, chat, card["id"], action="usual")
    search = reply["session"]["messages"][-1]
    assert search["kind"] == "search" and search["data"]["guided"] is None
    assert reply["session"]["messages"][-2]["data"]["state"] == "searched"
    assert wait_for(client, reply["job"]["id"])["status"] == "done"


def test_use_the_usual_for_the_rest_keeps_the_choices_made(client):
    chat, card = start(client)
    act(client, chat, card["id"], action="next", picks=["rye bread"])
    reply = act(client, chat, card["id"], action="usual")
    guided_search = reply["session"]["messages"][-1]["data"]["guided"]
    names = [i["name"] for i in guided_search["ingredients"]]
    assert names[0] == "rye bread" and "white bread" not in names
    assert {"cheddar cheese", "lettuce", "mayonnaise"} <= set(names)


def test_only_the_newest_build_can_change_and_start_over_ends_it(client):
    chat, card = start(client)
    session = say(client, chat, "start over")
    assert session["messages"][-1]["text"].startswith("OK, let's start over")
    act(client, chat, card["id"], expect=409, action="next")
    second = say(client, chat, "salad")["messages"][-1]
    assert second["kind"] == "build" and second["data"]["plan"]["dish"] == "salad"
    act(client, chat, card["id"], expect=409, action="next")


def test_a_dish_with_no_plan_falls_back_to_the_confirm_card(client):
    chat = client.post("/api/sessions").json()["id"]
    card = say(client, chat, "unicorn stew")["messages"][-1]
    assert card["kind"] in ("confirm", "text")
    if card["kind"] == "confirm":
        assert any("couldn't plan" in n for n in card["data"]["notes"])


def test_nothing_from_a_build_is_saved(client):
    chat, card = start(client)
    say(client, chat, "add pineapple and Dave's Killer Bread")
    act(client, chat, card["id"], action="finish")
    saved = saved_text()
    for word in ("pineapple", "Killer", "sandwich", "sourdough"):
        assert word not in saved, word


def test_bad_moves_are_refused(client):
    chat, card = start(client)
    act(client, chat, card["id"], expect=422, action="fly")
    act(client, chat, "m_nope", expect=404, action="next")
    act(client, chat, card["id"], expect=409, action="resolve", name="pineapple",
        choice="swap")
