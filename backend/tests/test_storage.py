"""What is saved where: accounts and encrypted preferences in Postgres, chats nowhere.
Against the test database (tests/pg.py)."""

import pytest
from conftest import (
    PASSWORD,
    PREFS,
    make_client,
    rows,
    saved_text,
    scratch_db,
    settings_for,
    signup,
)
from fastapi.testclient import TestClient

from app.chat import CHAT_ENDED
from app.crypto import KeyConfigError, new_key
from app.db import DatabaseConfigError
from app.engines.demo import DemoEngine
from app.main import COOKIE, create_app
from app.settings import Settings


def say(client, chat, text):
    return client.post(f"/api/sessions/{chat}/messages", json={"text": text})


def test_preferences_are_encrypted_in_the_database(user_client):
    saved = saved_text()
    for value in ("Demo shopper", "45202", "vegetarian", "gluten"):
        assert value not in saved, value
    [profile] = rows("SELECT * FROM profile.profiles")
    assert profile.key_version == "v1"
    assert user_client.get("/api/me").json()["zip"] == "45202"   # and read back


def test_chats_are_never_saved(user_client):
    chat = user_client.post("/api/sessions").json()["id"]
    assert say(user_client, chat, "hummus for a picnic").status_code == 200
    saved = saved_text()
    assert "hummus" not in saved and "picnic" not in saved and chat not in saved


def test_a_restart_keeps_the_login_and_preferences_but_not_the_chats():
    with make_client() as before:
        signup(before)
        chat = before.post("/api/sessions").json()["id"]
        say(before, chat, "hummus")
        token = before.cookies.get(COOKIE)
    with make_client(keep_data=True) as after:      # a new server on the same database
        after.cookies.set(COOKIE, token)
        me = after.get("/api/me").json()
        assert {k: me[k] for k in PREFS} == PREFS
        assert after.get("/api/sessions").json() == []
        ended = after.get(f"/api/sessions/{chat}")
        assert ended.status_code == 404 and ended.json()["detail"] == CHAT_ENDED


def test_a_profile_saved_with_another_key_is_an_error_not_blank_preferences():
    with make_client() as before:
        signup(before)
        token = before.cookies.get(COOKIE)
    with make_client(keep_data=True, profile_keys=f"v1:{new_key()}") as after:
        after.cookies.set(COOKIE, token)
        response = after.get("/api/me")
        assert response.status_code == 500
        assert response.json()["detail"] == "Your saved preferences can't be read."


def test_a_new_key_reads_old_profiles_and_saves_with_the_new_one():
    old = f"v1:{new_key()}"
    with make_client(profile_keys=old) as before:
        signup(before)
        token = before.cookies.get(COOKIE)
    with make_client(keep_data=True, profile_keys=f"v2:{new_key()},{old}") as after:
        after.cookies.set(COOKIE, token)
        assert after.get("/api/me").json()["zip"] == PREFS["zip"]
        assert after.put("/api/me/preferences", json=PREFS).status_code == 200
    [profile] = rows("SELECT * FROM profile.profiles")
    assert profile.key_version == "v2"


def test_a_username_is_taken_once_even_by_case():
    with make_client() as client:
        signup(client, "ana")
        taken = client.post("/api/auth/register",
                            json={"name": "A", "username": "Ana", "password": PASSWORD})
        assert taken.status_code == 409
    assert len(rows("SELECT * FROM auth.accounts")) == 1


@pytest.mark.parametrize("change", [{"profile_keys": ""}, {"profile_keys": "v1:short"},
                                    {"db_auth_url": None}, {"db_profile_url": None}])
def test_the_backend_wont_start_without_its_settings(change):
    settings = settings_for(scratch_db()).__dict__ | change
    with pytest.raises((KeyConfigError, DatabaseConfigError)):
        with TestClient(create_app(Settings(**settings), engine=DemoEngine(delay_s=0))):
            pass


def test_the_backend_wont_start_with_a_wrong_database_password():
    db = scratch_db()
    url = db.url("auth").replace(db.auth_password, "wrong-password")
    settings = settings_for(db).__dict__ | {"db_auth_url": url}
    with pytest.raises(Exception, match="password authentication failed"):
        with TestClient(create_app(Settings(**settings), engine=DemoEngine(delay_s=0))):
            pass


def test_logging_out_works_even_when_the_profile_cant_be_read():
    with make_client() as before:
        signup(before)
        token = before.cookies.get(COOKIE)
    with make_client(keep_data=True, profile_keys=f"v1:{new_key()}") as after:
        after.cookies.set(COOKIE, token)
        assert after.get("/api/me").status_code == 500
        assert after.post("/api/auth/logout").status_code == 204
        after.cookies.set(COOKIE, token)
        assert after.get("/api/me").status_code == 401          # the login really ended
