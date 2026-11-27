"""Throwaway Postgres databases for tests, on the Docker Postgres (backend/docker-compose.yml).

Each ScratchDatabase is a new database with its own two app users (random names and
passwords), migrated with the real migrations, so tests never touch the development
database or its pp_auth / pp_profile users. Tests that need one fail, with how to start
Postgres, when none answers at PANTRYPATH_TEST_DB_URL (default the Docker one).
"""

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL, make_url

BACKEND = Path(__file__).resolve().parents[1]
ADMIN_URL = os.environ.get(
    "PANTRYPATH_TEST_DB_URL",
    "postgresql+psycopg://pantrypath_owner:local-only-owner@127.0.0.1:5433/pantrypath")


def admin_engine() -> sa.Engine:
    return sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", poolclass=sa.NullPool)


def require_postgres() -> None:
    try:
        with admin_engine().connect():
            return
    except sa.exc.OperationalError:
        url = make_url(ADMIN_URL)
        pytest.fail(f"No Postgres at {url.host}:{url.port}. Start it from backend/ with "
                    "`docker compose up -d` (or set PANTRYPATH_TEST_DB_URL).", pytrace=False)


@dataclass
class ScratchDatabase:
    name: str
    auth_role: str
    profile_role: str
    auth_password: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    profile_password: str = field(default_factory=lambda: secrets.token_urlsafe(16))

    @classmethod
    def create(cls) -> "ScratchDatabase":
        require_postgres()
        suffix = secrets.token_hex(4)
        db = cls(name=f"pp_test_{suffix}", auth_role=f"pp_auth_{suffix}",
                 profile_role=f"pp_profile_{suffix}")
        with admin_engine().connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{db.name}"'))
        return db

    @property
    def env(self) -> dict[str, str]:
        """What the migration reads to create this database's two users."""
        return {"PANTRYPATH_DB_AUTH_ROLE": self.auth_role,
                "PANTRYPATH_DB_AUTH_PASSWORD": self.auth_password,
                "PANTRYPATH_DB_PROFILE_ROLE": self.profile_role,
                "PANTRYPATH_DB_PROFILE_PASSWORD": self.profile_password}

    @property
    def owner_url(self) -> URL:
        return make_url(ADMIN_URL).set(database=self.name)

    def url(self, role: str) -> str:
        user, password = {"auth": (self.auth_role, self.auth_password),
                          "profile": (self.profile_role, self.profile_password)}[role]
        return self.owner_url.set(username=user, password=password).render_as_string(
            hide_password=False)

    def alembic(self) -> Config:
        config = Config(str(BACKEND / "alembic.ini"))
        config.attributes["db_url"] = self.owner_url.render_as_string(hide_password=False)
        config.attributes["configure_logger"] = False
        return config

    def upgrade(self) -> None:
        with mock.patch.dict(os.environ, self.env):
            command.upgrade(self.alembic(), "head")

    def engine(self, role: str | None = None) -> sa.Engine:
        url = self.url(role) if role else self.owner_url
        return sa.create_engine(url, poolclass=sa.NullPool)

    def clear(self) -> None:
        """Every account (with its logins and profile) gone: a blank database."""
        with self.engine().begin() as conn:
            conn.execute(sa.text("TRUNCATE auth.accounts CASCADE"))

    def drop(self) -> None:
        with admin_engine().connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))
            for role in (self.auth_role, self.profile_role):
                conn.execute(sa.text(f'DROP ROLE IF EXISTS "{role}"'))
