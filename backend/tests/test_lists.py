"""Saved shopping lists (app/lists.py): built from a finished search without any Kroger
data, encrypted, ticked, found again; through the API with the demo engine."""

import json

import pytest
from conftest import PREFS, make_client, rows, saved_text, signup, wait_for
from fastapi.testclient import TestClient

from app.lists import from_search, kroger_names, scrub

RESULT = {
    "request": {"query": "grilled cheese", "zip": "45202", "avoid": ["peanuts"],
                "diet": "vegetarian"},
    "dish": {"name": "grilled cheese", "cuisine": "american", "source": "llm"},
    "stores": [{"store_id": "k1", "name": "Kroger - Kroger On the Rhine",
                "address": "100 E Court St, Cincinnati"}],
    "ingredients": [
        {"name": "swiss cheese", "category": "dairy", "role": "core", "products": [
            {"name": "Kroger® Swiss Sliced Cheese", "brand": "Kroger", "verdict": "safe"}]},
        {"name": "sourdough bread", "category": "bakery", "role": "core", "products": [
            {"name": "ACE Bakery® Sourdough Bistro", "brand": "ACE Bakery",
             "verdict": "unknown"}]},
    ],
}
ANSWER = {"ingredients": [
    {"name": "swiss cheese", "status": "buy", "product": "Kroger® Swiss Sliced Cheese",
     "store_id": "k1", "quantity": 4, "unit": "slices", "note": "Kroger® Swiss, aisle 12"},
    {"name": "sourdough bread", "status": "check_label", "product": None, "quantity": 1,
     "unit": "loaf", "note": None},
    {"name": "butter", "status": "substitute", "substitute": "vegan butter"}],
    "steps": ["Butter the bread.", "Add the Kroger® Swiss Sliced Cheese.",
              "Shop at Kroger On the Rhine.", "Toast 3 minutes a side."]}


def test_a_list_keeps_the_shoppers_data_and_no_kroger_data():
    saved = from_search(RESULT, ANSWER, {"servings": 2}, "guided")
    assert [(i["name"], i["category"], i["status"]) for i in saved["items"]] == [
        ("swiss cheese", "dairy", "buy"), ("sourdough bread", "bakery", "check_label"),
        ("butter", "other", "substitute")]
    assert saved["items"][0]["quantity"] == 4 and saved["items"][2]["substitute"] == \
        "vegan butter"
    assert saved["steps"] == ["Butter the bread.", "Toast 3 minutes a side."]
    text = json.dumps(saved).lower()
    for name in kroger_names(RESULT):
        assert name not in text, name
    assert "aisle" not in text and "k1" not in text


def test_without_an_answer_safe_ingredients_are_buy_and_the_rest_read_the_label():
    saved = from_search(RESULT, None, {}, "search")
    assert [i["status"] for i in saved["items"]] == ["buy", "check_label"]
    assert saved["steps"] == []


def test_scrub_ignores_very_short_names():
    assert scrub(["Use ace."], {"ace"}) == []
    assert "ace" not in kroger_names({"ingredients": [{"products": [{"name": "Ace"}]}]})


# ------------------------------------------------------------ through the API

@pytest.fixture
def client():
    with make_client() as c:
        signup(c, "ana", prefs=PREFS)
        yield c


def search(client, text="pad thai for 2"):
    chat = client.post("/api/sessions").json()["id"]
    card = client.post(f"/api/sessions/{chat}/messages",
                       json={"text": text}).json()["session"]["messages"][-1]
    body = {"message_id": card["id"], **card["data"]["request"], "servings": 2}
    job_id = client.post(f"/api/sessions/{chat}/search", json=body).json()["job"]["id"]
    return chat, wait_for(client, job_id)


def test_a_finished_search_saves_its_list(client):
    chat, job = search(client)
    assert job["list_id"] and not job["list_error"]
    saved = client.get(f"/api/lists/{job['list_id']}").json()
    assert saved["dish"] == job["result"]["dish"]["name"] and saved["servings"] == 2
    assert {i["name"] for i in saved["items"]} == {i["name"] for i in job["answer"]["ingredients"]}
    message = client.get(f"/api/sessions/{chat}").json()["messages"][-1]
    assert message["data"]["list_id"] == job["list_id"]
    [summary] = client.get("/api/lists").json()
    assert summary == {"id": job["list_id"], "dish": saved["dish"],
                       "created_at": summary["created_at"], "items": len(saved["items"]),
                       "ticked": 0}


def test_no_store_data_reaches_the_database_or_the_list(client):
    _, job = search(client)
    names = {p["name"] for i in job["result"]["ingredients"] for p in i["products"]}
    names |= {s["name"] for s in job["result"]["stores"]}
    stored = saved_text() + json.dumps(client.get(f"/api/lists/{job['list_id']}").json())
    assert names and not [n for n in names if n in stored]
    [row] = rows("SELECT * FROM profile.lists")
    assert job["result"]["dish"]["name"].encode() not in bytes(row.list_encrypted)


def test_ticks_are_saved(client):
    _, job = search(client)
    saved = client.get(f"/api/lists/{job['list_id']}").json()
    item = saved["items"][0]["id"]
    ticked = client.put(f"/api/lists/{job['list_id']}/items/{item}", json={"ticked": True})
    assert ticked.status_code == 200 and ticked.json()["items"][0]["ticked"] is True
    assert client.get("/api/lists").json()[0]["ticked"] == 1
    assert client.put(f"/api/lists/{job['list_id']}/items/i999",
                      json={"ticked": True}).status_code == 404


def test_find_products_again_searches_the_items_without_saving_anything(client):
    _, job = search(client)
    saved = client.get(f"/api/lists/{job['list_id']}").json()
    found = client.post(f"/api/lists/{job['list_id']}/products")
    assert found.status_code == 200
    again = wait_for(client, found.json()["id"])
    assert again["status"] == "done" and again["answer"] is None and not again["list_id"]
    expected = [i["substitute"] if i["status"] == "substitute" else i["name"]
                for i in saved["items"] if i["status"] != "skip"]
    assert [i["name"] for i in again["result"]["ingredients"]] == expected
    assert len(client.get("/api/lists").json()) == 1          # not a new list


def test_lists_are_private_and_can_be_deleted(client):
    _, job = search(client)
    bob = TestClient(client.app)
    signup(bob, "bob", prefs=None)
    assert bob.get("/api/lists").json() == []
    for method, path in (("GET", f"/api/lists/{job['list_id']}"),
                         ("DELETE", f"/api/lists/{job['list_id']}"),
                         ("POST", f"/api/lists/{job['list_id']}/products"),
                         ("GET", "/api/lists/not-a-uuid")):
        assert bob.request(method, path).status_code == 404, path
    assert client.delete(f"/api/lists/{job['list_id']}").status_code == 204
    assert client.get("/api/lists").json() == []


def test_deleting_the_account_deletes_its_lists(client):
    search(client)
    assert client.post("/api/me/delete", json={"password": "correct horse battery"}) \
        .status_code == 204
    assert rows("SELECT * FROM profile.lists") == []


def test_searching_again_keeps_the_same_saved_list(client):
    chat, job = search(client)
    message = client.get(f"/api/sessions/{chat}").json()["messages"][-1]
    again = client.post(f"/api/sessions/{chat}/messages/{message['id']}/rerun").json()
    rerun = wait_for(client, again["job"]["id"])
    assert rerun["list_id"] == job["list_id"]
    assert len(client.get("/api/lists").json()) == 1
