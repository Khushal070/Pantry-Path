import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.engines.demo import DemoEngine
from app.main import create_app
from app.settings import Settings

PASSWORD = "correct horse battery"
# The old demo profile's preferences: shows how saved preferences meet the chat
PREFS = {"name": "Demo shopper", "zip": "45202", "avoid": ["milk", "gluten"],
         "diet": "vegetarian", "servings": 4, "needs": []}


def make_client(tmp_path, **settings: Any) -> TestClient:
    config = Settings(data_dir=tmp_path / "data", demo_delay_s=0, **settings)
    return TestClient(create_app(config, engine=DemoEngine(delay_s=0)))


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
def client(tmp_path) -> Iterator[TestClient]:
    with make_client(tmp_path) as c:
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
