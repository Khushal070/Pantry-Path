import time
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pg import ScratchDatabase

from app.crypto import new_key
from app.engines.base import Engine
from app.engines.demo import DemoEngine
from app.main import create_app
from app.settings import Settings

PASSWORD = "correct horse battery"
# The old demo profile's preferences: shows how saved preferences meet the chat
PREFS = {"name": "Demo shopper", "zip": "45202", "avoid": ["milk", "gluten"],
         "diet": "vegetarian", "servings": 4, "needs": []}


PROFILE_KEYS = f"v1:{new_key()}"
_db: ScratchDatabase | None = None


def scratch_db() -> ScratchDatabase:
    """One migrated throwaway database for the whole test run (made on first use, so
    tests that need no database run without Postgres)."""
    global _db
    if _db is None:
        _db = ScratchDatabase.create()
        _db.upgrade()
    return _db


def pytest_sessionfinish(session, exitstatus) -> None:
    if _db is not None:
        _db.drop()


def rows(query: str) -> list[sa.Row]:
    """Run SQL on the test database as its owner (who sees every table)."""
    with scratch_db().engine().begin() as conn:
        result = conn.execute(sa.text(query))
        return list(result) if result.returns_rows else []


def saved_text() -> str:
    """Everything the database holds, as text: to check what is never saved."""
    return "\n".join(str(r) for table in ("auth.accounts", "auth.logins", "profile.profiles",
                                          "profile.lists")
                     for r in rows(f"SELECT * FROM {table}"))


def settings_for(db: ScratchDatabase, **settings: Any) -> Settings:
    # The confirm-card flow unless a test asks for guided builds (test_guided_api.py)
    settings.setdefault("guided_builds", False)
    return Settings(db_auth_url=db.url("auth"), db_profile_url=db.url("profile"),
                    profile_keys=settings.pop("profile_keys", PROFILE_KEYS), demo_delay_s=0,
                    **settings)


def make_client(engine: Engine | None = None, keep_data: bool = False,
                **settings: Any) -> TestClient:
    """A client of a new app on the test database, emptied first unless keep_data (a
    restart of the same server)."""
    db = scratch_db()
    if not keep_data:
        db.clear()
    return TestClient(create_app(settings_for(db, **settings),
                                 engine=engine or DemoEngine(delay_s=0)))


def signup(client: TestClient, username: str = "demo", prefs: dict[str, Any] | None = PREFS,
           password: str = PASSWORD) -> dict[str, Any]:
    """Register (which logs this client in) and save preferences."""
    response = client.post("/api/auth/register", json={
        "name": (prefs or {}).get("name", username), "username": username,
        "password": password})
    assert response.status_code == 201, response.text
    if prefs:
        response = client.put("/api/me/preferences", json=prefs)
        assert response.status_code == 200, response.text
    return response.json()


def login(client: TestClient, username: str, password: str = PASSWORD):
    return client.post("/api/auth/login", json={"username": username, "password": password})


@pytest.fixture
def client() -> Iterator[TestClient]:
    with make_client() as c:
        yield c


@pytest.fixture
def user_client(client) -> TestClient:
    """A client logged in as "demo", with the demo preferences."""
    signup(client)
    return client


def wait_for(client: TestClient, job_id: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        if response.status_code != 200 or response.json()["status"] != "running":
            return response.json() | {"http": response.status_code}
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} still running after {timeout}s")
