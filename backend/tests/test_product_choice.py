"""Choosing products in a guided build, favourite brands, and the final list of picks,
through the API with the demo engine."""

import time

import pytest
from conftest import make_client, signup, wait_for

PREFS = {"name": "Ana", "zip": "45202", "avoid": ["peanuts"], "diet": "vegetarian",
         "servings": 2, "needs": []}


@pytest.fixture
def client():
    with make_client(guided_builds=True) as c:
        signup(c, "ana", prefs=PREFS)
        yield c


def act(client, chat, card_id, expect=200, **body):
    response = client.post(f"/api/sessions/{chat}/builds/{card_id}", json=body)
    assert response.status_code == expect, response.text
    return response.json()


def ready_build(client, chat, card_id, timeout=5.0):
    """The build card once its products have arrived (they're fetched in the background)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        card = next(m for m in client.get(f"/api/sessions/{chat}").json()["messages"]
                    if m["id"] == card_id)
        if card["data"]["products"]["status"] != "loading":
            return card
        time.sleep(0.02)
    raise AssertionError("products still loading")


def start(client, text="sandwich"):
    chat = client.post("/api/sessions").json()["id"]
    card = client.post(f"/api/sessions/{chat}/messages",
                       json={"text": text}).json()["session"]["messages"][-1]
    assert card["kind"] == "build"
    assert card["data"]["products"]["status"] == "loading"
    return chat, ready_build(client, chat, card["id"])


def finish(client, chat, card):
    return act(client, chat, card["id"], action="finish")["session"]["messages"][-1]


def search(client, chat, summary):
    body = {"message_id": summary["id"], **summary["data"]["request"], "servings": 2}
    reply = client.post(f"/api/sessions/{chat}/search", json=body)
    assert reply.status_code == 200, reply.text
    return wait_for(client, reply.json()["job"]["id"])


def test_every_option_gets_a_pick_at_the_main_store(client):
    chat, card = start(client)
    build = card["data"]
    assert build["products"]["status"] == "ready" and build["products"]["main"]
    bread = build["choices"]["white bread"]
    assert bread["pick"]["verdict"] in ("safe", "unknown")
    assert build["chosen"]["white bread"] == bread["pick"]["key"]
    options = {o["name"] for s in build["plan"]["steps"] for o in s["options"]}
    assert options <= set(build["choices"])


def test_changing_a_pick_and_the_final_list_buys_exactly_it(client):
    chat, card = start(client)
    bread = card["data"]["choices"]["white bread"]
    other = bread["alternatives"][0]
    act(client, chat, card["id"], action="choose", name="white bread", key=other["key"])
    act(client, chat, card["id"], expect=409, action="choose", name="white bread",
        key="nope|nope")
    summary = finish(client, chat, card)
    assert summary["data"]["guided"]["choices"]["white bread"]["key"] == other["key"]
    job = search(client, chat, summary)
    assert job["choices"]["white bread"]["name"] == other["name"]
    bought = next(i for i in job["answer"]["ingredients"] if i["name"] == "white bread")
    assert bought["product"] == other["name"] and bought["store_id"] == other["store_id"]


def test_picked_brands_become_favourites_and_come_back_next_time(client):
    chat, card = start(client)
    brand = card["data"]["choices"]["white bread"]["pick"]["brand"]
    summary = finish(client, chat, card)
    assert summary["data"]["guided"]["favourites_new"]["white bread"] == brand
    job = search(client, chat, summary)
    assert client.get("/api/me").json()["favourites"]["white bread"] == brand
    saved = client.get(f"/api/lists/{job['list_id']}").json()
    assert next(i for i in saved["items"] if i["name"] == "white bread")["brand"] == brand
    _, again = start(client)
    choice = again["data"]["choices"]["white bread"]
    assert choice["favourite"] == brand and not choice["favourite_missing"]
    assert brand.lower() in (choice["pick"]["brand"] + choice["pick"]["name"]).lower()


def test_a_different_brand_asks_before_replacing_the_favourite(client):
    """(Demo products contain whatever was searched, so a favourite is always "found";
    test_choices.py covers a missing one.)"""
    assert client.put("/api/me/favourites",
                      json={"item": "white bread", "brand": "Old Mill"}).status_code == 200
    chat, card = start(client)
    choice = card["data"]["choices"]["white bread"]
    assert choice["favourite"] == "Old Mill"
    other = next(v for v in [choice["pick"], *choice["alternatives"]]
                 if v["brand"] != "Old Mill")
    act(client, chat, card["id"], action="choose", name="white bread", key=other["key"])
    summary = finish(client, chat, card)
    ask = summary["data"]["guided"]["favourites_ask"]
    assert ask == [{"item": "white bread", "old": "Old Mill", "new": other["brand"]}]
    search(client, chat, summary)
    assert client.get("/api/me").json()["favourites"]["white bread"] == "Old Mill"


def test_a_family_brand_is_offered_first_not_picked(client):
    client.put("/api/me/favourites", json={"item": "cheddar cheese", "brand": "Old Mill"})
    _, card = start(client)
    swiss = card["data"]["choices"]["swiss cheese"]
    assert swiss["hint_brand"] == "Old Mill" and swiss["hint"]
    assert card["data"]["chosen"]["swiss cheese"] != swiss["hint"]["key"]


def test_favourites_survive_a_preferences_save_and_can_be_removed(client):
    client.put("/api/me/favourites", json={"item": "white bread", "brand": "Old Mill"})
    assert client.put("/api/me/preferences", json=PREFS).status_code == 200
    assert client.get("/api/me").json()["favourites"] == {"white bread": "Old Mill"}
    client.put("/api/me/favourites", json={"item": "white bread", "brand": None})
    assert client.get("/api/me").json()["favourites"] == {}


def test_an_addition_gets_products_too(client):
    chat, card = start(client)
    act(client, chat, card["id"], action="add", text="pineapple")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        card = ready_build(client, chat, card["id"])
        if "pineapple" in card["data"]["choices"]:
            break
        time.sleep(0.02)
    assert card["data"]["choices"]["pineapple"]["pick"]
    summary = finish(client, chat, card)
    assert "pineapple" in summary["data"]["guided"]["choices"]


def test_produce_brands_are_not_remembered(client):
    chat, card = start(client)
    summary = finish(client, chat, card)
    new = summary["data"]["guided"]["favourites_new"]
    assert "lettuce" not in new and "tomatoes" not in new and "white bread" in new


def test_adding_more_at_the_summary_reopens_the_build_with_the_items(client):
    chat, card = start(client)
    summary = finish(client, chat, card)
    assert summary["data"]["build_id"] == card["id"]
    reply = client.post(f"/api/sessions/{chat}/messages/{summary['id']}/add",
                        json={"text": "ketchup and peanut butter"})
    assert reply.status_code == 200, reply.text
    messages = reply.json()["session"]["messages"]
    build = messages[-1]
    assert build["id"] == card["id"] and build["data"]["state"] == "choosing"
    assert all(m["id"] != summary["id"] for m in messages)            # the old summary is gone
    status = {a["name"]: a["status"] for a in build["data"]["additions"]}
    assert status == {"ketchup": "ok", "peanut butter": "clash"}
    act(client, chat, card["id"], action="resolve", name="peanut butter", choice="remove")
    ready_build(client, chat, card["id"])
    again = finish(client, chat, card)
    assert "ketchup" in [i["name"] for i in again["data"]["guided"]["ingredients"]]
    gone = client.post(f"/api/sessions/{chat}/messages/{summary['id']}/add",
                       json={"text": "salsa"})
    assert gone.status_code == 409


def test_typing_at_the_summary_adds_instead_of_starting_over(client):
    chat, card = start(client)
    finish(client, chat, card)
    session = client.post(f"/api/sessions/{chat}/messages",
                          json={"text": "add ketchup"}).json()["session"]
    assert session["messages"][-2]["text"] == "add ketchup"
    build = session["messages"][-1]
    assert build["id"] == card["id"] and build["data"]["state"] == "choosing"
    assert [a["name"] for a in build["data"]["additions"]] == ["ketchup"]


def test_a_favourite_found_by_its_brand_is_bought_at_the_end(client):
    client.put("/api/me/favourites", json={"item": "white bread", "brand": "Old Mill"})
    chat, card = start(client)
    pick = card["data"]["choices"]["white bread"]["pick"]
    assert pick["term"] == "Old Mill white bread"         # found by the brand search
    summary = finish(client, chat, card)
    bread = next(i for i in summary["data"]["guided"]["ingredients"] if i["name"] == "white bread")
    assert bread["term"] == "Old Mill white bread"
    job = search(client, chat, summary)
    bought = next(i for i in job["answer"]["ingredients"] if i["name"] == "white bread")
    assert bought["product"] == pick["name"]
    assert "white bread" in [i["name"] for i in job["result"]["ingredients"]]


def test_typing_into_a_build_that_just_finished_is_a_conflict_and_keeps_the_cards(client):
    from app.chat import ConflictError

    chat, card = start(client)
    act(client, chat, card["id"], action="usual")
    service = client.app.state.chat
    user = client.app.state.accounts.find("ana")
    with pytest.raises(ConflictError):
        service._typed_into_build(user, chat, "pineapple", card["id"])
    messages = client.get(f"/api/sessions/{chat}").json()["messages"]
    assert card["id"] in [m["id"] for m in messages]


def test_editing_the_summary_keeps_dish_and_zip_and_a_new_allergy_drops_the_picks(client):
    chat, card = start(client)
    summary = finish(client, chat, card)
    request = summary["data"]["request"]
    body = {"message_id": summary["id"], "query": "pad thai", "zip": "10001",
            "avoid": [*request["avoid"], "milk"], "diet": request["diet"], "servings": 2}
    reply = client.post(f"/api/sessions/{chat}/search", json=body).json()
    searched = reply["session"]["messages"][-1]["data"]
    assert searched["request"]["query"] == request["query"]            # not "pad thai"
    assert searched["request"]["zip"] == request["zip"]                # not 10001
    assert "milk" in searched["request"]["avoid"]
    assert not searched["guided"]["choices"] and not searched["guided"]["favourites_new"]
    job = wait_for(client, reply["job"]["id"])
    assert not job["choices"]
    assert client.get("/api/me").json()["favourites"] == {}


def test_more_than_200_favourites_drops_the_oldest(client):
    accounts = client.app.state.accounts
    user = accounts.find("ana")
    accounts.set_favourites(user["id"], {f"item {i:03}": "Brand" for i in range(200)})
    response = client.put("/api/me/favourites", json={"item": "new item", "brand": "Fresh Co"})
    assert response.status_code == 200
    favourites = response.json()["favourites"]
    assert len(favourites) == 200 and "new item" in favourites and "item 000" not in favourites


def test_favourites_saved_at_the_same_time_are_all_kept(client):
    from concurrent.futures import ThreadPoolExecutor

    accounts = client.app.state.accounts
    user_id = accounts.find("ana")["id"]
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda i: accounts.set_favourites(user_id, {f"thing {i}": "B"}),
                      range(16)))
    assert len(client.get("/api/me").json()["favourites"]) == 16
