"""Accounts: password hashing, logins and logouts, password changes, deleting an account,
and that one user never reaches another's chats or results."""

import json

from conftest import PASSWORD, login, make_client, signup, wait_for
from fastapi.testclient import TestClient

from app.auth import Accounts, hash_password, token_key, verify_password
from app.store import JsonStore

COOKIE = "pantrypath_login"


def test_password_hashes():
    stored = hash_password("hunter2hunter2")
    assert stored.startswith("scrypt$") and "hunter2" not in stored
    assert verify_password("hunter2hunter2", stored)
    assert not verify_password("hunter2hunter3", stored)
    assert hash_password("hunter2hunter2") != stored          # a new salt every time
    assert not verify_password("x", "not a hash")
    assert not verify_password("x", "md5$1$2$3$4$5")


def test_register_logs_in_with_a_safe_cookie(client, tmp_path):
    response = client.post("/api/auth/register",
                           json={"name": "Ana", "username": "  Ana.B ", "password": PASSWORD})
    assert response.status_code == 201
    cookie = response.headers["set-cookie"].lower()
    assert f"{COOKIE}=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    user = response.json()
    assert user["username"] == "ana.b" and "password_hash" not in user
    assert client.get("/api/me").json()["id"] == user["id"]
    saved = (tmp_path / "data" / "users" / f"{user['id']}.json").read_text()
    assert PASSWORD not in saved and "scrypt$" in saved
    token = client.cookies.get(COOKIE)
    logins = list((tmp_path / "data" / "logins").glob("*.json"))
    assert [p.stem for p in logins] == [token_key(token)]
    assert token not in logins[0].read_text()      # only the token's hash is kept


def test_usernames_are_unique_and_checked(client):
    signup(client, "ana")
    taken = client.post("/api/auth/register",
                        json={"name": "A", "username": "ANA", "password": PASSWORD})
    assert taken.status_code == 409
    for bad in ("ab", "has space", "../x", "a" * 33, "-dash"):
        response = client.post("/api/auth/register",
                               json={"name": "A", "username": bad, "password": PASSWORD})
        assert response.status_code == 422, bad
    short = client.post("/api/auth/register",
                        json={"name": "A", "username": "short", "password": "1234567"})
    assert short.status_code == 422


def test_login_and_logout(client):
    signup(client, "ana")
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/me").status_code == 401
    wrong, unknown = login(client, "ana", "wrong password"), login(client, "nobody")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()          # never says which part was wrong
    assert login(client, " ANA ").status_code == 200
    assert client.get("/api/me").json()["username"] == "ana"


def test_a_logged_out_token_stops_working(client):
    signup(client, "ana")
    token = client.cookies.get(COOKIE)
    client.post("/api/auth/logout")
    assert client.cookies.get(COOKIE) is None
    client.cookies.set(COOKIE, token)              # replaying the old cookie
    assert client.get("/api/me").status_code == 401


def test_switching_users_in_one_browser(client):
    signup(client, "ana")
    chat = client.post("/api/sessions").json()["id"]
    client.post("/api/auth/logout")
    signup(client, "bob")
    assert client.get("/api/sessions").json() == []
    client.post("/api/auth/logout")
    login(client, "ana")
    assert [s["id"] for s in client.get("/api/sessions").json()] == [chat]


def test_everything_but_config_and_auth_needs_a_login(client):
    assert client.get("/api/config").status_code == 200
    for method, path in (("GET", "/api/me"), ("PUT", "/api/me/preferences"),
                         ("POST", "/api/me/password"), ("POST", "/api/me/delete"),
                         ("GET", "/api/sessions"), ("POST", "/api/sessions"),
                         ("GET", "/api/sessions/s_x"), ("DELETE", "/api/sessions/s_x"),
                         ("POST", "/api/sessions/s_x/messages"),
                         ("POST", "/api/sessions/s_x/search"),
                         ("POST", "/api/sessions/s_x/messages/m_x/rerun"),
                         ("GET", "/api/jobs/j_x")):
        assert client.request(method, path, json={}).status_code == 401, path


def test_too_many_wrong_passwords_pause_logins(client):
    signup(client, "ana")
    client.post("/api/auth/logout")
    for _ in range(5):
        assert login(client, "ana", "wrong password").status_code == 401
    assert login(client, "ana").status_code == 429       # even the right one, for a while
    assert login(client, "bob", "anything").status_code == 401   # other names unaffected


def test_expired_logins_end(tmp_path):
    with make_client(tmp_path) as client:
        signup(client, "ana", prefs=None)
        path = next((tmp_path / "data" / "logins").glob("*.json"))
        record = json.loads(path.read_text()) | {"expires_at": "2020-01-01T00:00:00+00:00"}
        path.write_text(json.dumps(record))
        assert client.get("/api/me").status_code == 401
        assert not path.exists()


def test_expired_logins_are_purged(tmp_path):
    accounts = Accounts(JsonStore(tmp_path), login_days=0)   # every login expires at once
    accounts.start_login("u_1")
    assert accounts.purge_expired() == 1
    assert not list((tmp_path / "logins").glob("*.json"))


def test_changing_the_password_logs_out_other_browsers(tmp_path):
    with make_client(tmp_path) as phone:
        signup(phone, "ana")
        laptop = TestClient(phone.app)                 # a second browser, its own cookies
        assert login(laptop, "ana").status_code == 200
        change = {"current_password": PASSWORD, "new_password": "a new password"}
        assert phone.post("/api/me/password",
                          json={**change, "current_password": "wrong"}).status_code == 403
        assert phone.post("/api/me/password",
                          json={**change, "new_password": "short"}).status_code == 422
        assert phone.post("/api/me/password", json=change).status_code == 204
        assert phone.get("/api/me").status_code == 200    # this browser stays logged in
        assert laptop.get("/api/me").status_code == 401   # the other one is logged out
        phone.post("/api/auth/logout")
        assert login(phone, "ana").status_code == 401
        assert login(phone, "ana", "a new password").status_code == 200


def test_deleting_an_account_removes_its_data(tmp_path):
    with make_client(tmp_path) as client:
        user = signup(client, "ana")
        chat = client.post("/api/sessions").json()["id"]
        assert client.post("/api/me/delete", json={"password": "wrong"}).status_code == 403
        assert client.post("/api/me/delete", json={"password": PASSWORD}).status_code == 204
        assert client.get("/api/me").status_code == 401
        assert login(client, "ana").status_code == 401
    data = tmp_path / "data"
    assert not (data / "users" / f"{user['id']}.json").exists()
    assert not (data / "sessions" / f"{chat}.json").exists()
    assert not list((data / "logins").glob("*.json"))


def test_users_never_reach_each_others_chats_or_results(tmp_path):
    with make_client(tmp_path) as ana:
        signup(ana, "ana")
        chat = ana.post("/api/sessions").json()["id"]
        card = ana.post(f"/api/sessions/{chat}/messages",
                        json={"text": "hummus"}).json()["session"]["messages"][-1]
        body = {"message_id": card["id"], **card["data"]["request"], "servings": None}
        job_id = ana.post(f"/api/sessions/{chat}/search", json=body).json()["job"]["id"]
        wait_for(ana, job_id)
        search_id = ana.get(f"/api/sessions/{chat}").json()["messages"][-1]["id"]

        bob = TestClient(ana.app)
        signup(bob, "bob", prefs=None)
        assert bob.get("/api/sessions").json() == []
        for method, path, payload in (
                ("GET", f"/api/sessions/{chat}", None),
                ("DELETE", f"/api/sessions/{chat}", None),
                ("POST", f"/api/sessions/{chat}/messages", {"text": "hi"}),
                ("POST", f"/api/sessions/{chat}/search", body),
                ("POST", f"/api/sessions/{chat}/messages/{search_id}/rerun", None),
                ("GET", f"/api/jobs/{job_id}", None)):
            assert bob.request(method, path, json=payload).status_code == 404, path
        assert ana.get(f"/api/sessions/{chat}").status_code == 200    # untouched
        assert ana.get(f"/api/jobs/{job_id}").status_code == 200
