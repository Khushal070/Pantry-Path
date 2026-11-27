from conftest import PREFS, make_client, saved_text, signup, wait_for


def new_chat(client):
    return client.post("/api/sessions").json()["id"]


def say(client, session_id, text):
    response = client.post(f"/api/sessions/{session_id}/messages", json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()["session"]["messages"][-1]


def confirm(client, session_id, card, **changes):
    body = {"message_id": card["id"], **card["data"]["request"],
            "servings": card["data"]["servings"], **changes}
    return client.post(f"/api/sessions/{session_id}/search", json=body)


def test_config_is_public(client):
    config = client.get("/api/config").json()
    assert config["mode"] == "demo"
    assert [a["id"] for a in config["allergens"]][:2] == ["milk", "egg"]


def test_preferences_are_validated(user_client):
    good = user_client.put("/api/me/preferences",
                           json={**PREFS, "zip": "", "avoid": ["sesame", "egg"],
                                 "needs": ["halal"]})
    assert good.status_code == 200
    assert good.json()["zip"] is None and good.json()["avoid"] == ["egg", "sesame"]
    for bad in ({"zip": "123"}, {"avoid": ["nuts"]}, {"needs": ["vegan"]}, {"name": " "},
                {"servings": 0}):
        assert user_client.put("/api/me/preferences", json={**PREFS, **bad}).status_code == 422


def test_chat_to_shopping_list(user_client):
    client = user_client
    chat = new_chat(client)
    card = say(client, chat, "I feel like quiche tonight, no nuts")
    assert card["kind"] == "confirm"
    draft = card["data"]
    assert draft["state"] == "check"            # "nuts": peanuts too?
    assert draft["request"]["avoid"] == ["milk", "tree_nuts", "peanuts", "gluten"]
    assert draft["locked"] == ["milk", "gluten"]
    response = confirm(client, chat, card)
    assert response.status_code == 200
    job = wait_for(client, response.json()["job"]["id"])
    assert job["status"] == "done"
    assert job["result"]["request"]["avoid"] == ["milk", "tree_nuts", "peanuts", "gluten"]
    assert job["answer"]["ingredients"] and job["warnings"]
    session = client.get(f"/api/sessions/{chat}").json()
    search = session["messages"][-1]
    assert search["kind"] == "search"
    assert search["data"]["status"] == "done" and search["data"]["dish"] == "quiche lorraine"
    assert session["title"] == "Quiche lorraine"
    assert session["context_start"] == len(session["messages"])
    summaries = client.get("/api/sessions").json()
    assert summaries[0]["id"] == chat and summaries[0]["title"] == "Quiche lorraine"


def test_saved_preferences_change_the_next_card(user_client):
    chat = new_chat(user_client)
    assert say(user_client, chat, "hummus")["data"]["request"]["avoid"] == ["milk", "gluten"]
    user_client.put("/api/me/preferences", json={**PREFS, "avoid": ["sesame"], "diet": "none"})
    card = say(user_client, new_chat(user_client), "hummus")
    assert card["data"]["request"]["avoid"] == ["sesame"]
    assert card["data"]["request"]["diet"] == "none"


def test_store_results_are_never_saved():
    with make_client() as client:
        signup(client)
        chat = new_chat(client)
        card = say(client, chat, "pad thai for 2")
        job = wait_for(client, confirm(client, chat, card).json()["job"]["id"])
        names = {p["name"] for i in job["result"]["ingredients"] for p in i["products"]}
        names |= {s["name"] for s in job["result"]["stores"]}
        # the database, and the profile once decrypted
        saved = saved_text() + str(client.app.state.accounts.find("demo"))
    assert names and not [n for n in names if n in saved]


def test_the_server_reapplies_saved_restrictions(user_client):
    chat = new_chat(user_client)
    card = say(user_client, chat, "banana bread")
    response = confirm(user_client, chat, card, avoid=[], diet="none")
    request = response.json()["session"]["messages"][-1]["data"]["request"]
    assert request["avoid"] == ["milk", "gluten"] and request["diet"] == "vegetarian"


def test_the_searched_card_shows_what_was_searched(user_client):
    chat = new_chat(user_client)
    card = say(user_client, chat, "banana bread")
    assert "near 45202" in card["text"]                    # first reading: the saved ZIP
    session = confirm(user_client, chat, card, zip="10001", diet="vegan",
                      servings=2).json()["session"]
    searched = next(m for m in session["messages"] if m["id"] == card["id"])
    # the card's edits, plus the saved allergies the server always applies
    expected = "Banana bread near 10001, vegan, without milk and gluten, for 2"
    assert searched["text"] == expected and searched["data"]["submitted"] is True
    reopened = user_client.get(f"/api/sessions/{chat}").json()
    assert next(m for m in reopened["messages"] if m["id"] == card["id"])["text"] == expected


def test_only_the_newest_card_can_be_searched(user_client):
    chat = new_chat(user_client)
    old = say(user_client, chat, "brownies")
    new = say(user_client, chat, "actually no eggs")
    assert new["data"]["request"]["avoid"] == ["milk", "egg", "gluten"]
    assert confirm(user_client, chat, old).status_code == 409
    assert confirm(user_client, chat, new).status_code == 200
    assert confirm(user_client, chat, new).status_code == 409   # once only


def test_asks_for_a_zip_when_neither_the_chat_nor_the_preferences_have_one(client):
    signup(client, "nozip", prefs=None)
    chat = new_chat(client)
    question = say(client, chat, "chili for 6")
    assert question["kind"] == "text" and "ZIP" in question["text"]
    card = say(client, chat, "45202")
    assert card["kind"] == "confirm"
    assert card["data"]["request"]["zip"] == "45202" and card["data"]["servings"] == 6


def test_asks_for_a_dish(user_client):
    reply = say(user_client, new_chat(user_client), "hello there")
    assert reply["kind"] == "text" and "Which dish" in reply["text"]


def test_a_new_request_after_a_search_starts_afresh(user_client):
    chat = new_chat(user_client)
    card = say(user_client, chat, "pad thai, no eggs")
    wait_for(user_client, confirm(user_client, chat, card).json()["job"]["id"])
    card = say(user_client, chat, "now hummus")
    assert card["data"]["request"]["query"] == "hummus"
    assert "egg" not in card["data"]["request"]["avoid"]   # the old chat isn't read again


def test_expired_results_can_be_searched_again():
    with make_client(result_ttl_minutes=0) as client:
        signup(client)
        chat = new_chat(client)
        card = say(client, chat, "hummus")
        job_id = confirm(client, chat, card).json()["job"]["id"]
        assert wait_for(client, job_id)["http"] == 404     # gone as soon as it finished
        search = client.get(f"/api/sessions/{chat}").json()["messages"][-1]
        rerun = client.post(f"/api/sessions/{chat}/messages/{search['id']}/rerun")
        assert rerun.status_code == 200
        assert rerun.json()["job"]["id"] != job_id


def test_a_failed_search_says_why(user_client):
    chat = new_chat(user_client)
    card = say(user_client, chat, "falafel")
    # the demo knows falafel; ask for a dish it doesn't by editing the card
    job = wait_for(user_client,
                   confirm(user_client, chat, card, query="unicorn stew").json()["job"]["id"])
    assert job["status"] == "failed" and job["error_kind"] == "not_found"
    search = user_client.get(f"/api/sessions/{chat}").json()["messages"][-1]
    assert search["data"]["status"] == "failed" and "unicorn stew" in search["data"]["error"]
    assert search["data"]["error_kind"] == "not_found"   # kept, so a reopened chat says why


def test_bad_requests(user_client):
    chat = new_chat(user_client)
    card = say(user_client, chat, "falafel")
    assert confirm(user_client, chat, card, zip="12").status_code == 422
    assert user_client.post(f"/api/sessions/{chat}/messages",
                            json={"text": "  "}).status_code == 422
    assert user_client.get("/api/sessions/s_missing").status_code == 404
    assert user_client.get("/api/sessions/..%2Fusers%2Fx").status_code == 404
    assert user_client.delete(f"/api/sessions/{chat}").status_code == 204
    assert user_client.get(f"/api/sessions/{chat}").status_code == 404

