"""The first migration, against a real Postgres (backend/docker-compose.yml).

Each test gets its own throwaway database and its own two users (tests/pg.py), so the
development database and its pp_auth / pp_profile users are never touched. The users'
names and passwords reach the migration through the environment, as they do in a deploy.
"""

import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from pg import ScratchDatabase, admin_engine
from sqlalchemy.exc import IntegrityError, ProgrammingError


@pytest.fixture
def db(monkeypatch) -> Iterator[ScratchDatabase]:
    scratch = ScratchDatabase.create()
    for name, value in scratch.env.items():
        monkeypatch.setenv(name, value)
    try:
        yield scratch
    finally:
        scratch.drop()


def upgrade(db: ScratchDatabase) -> None:
    """With whatever the test left in the environment (unlike ScratchDatabase.upgrade)."""
    command.upgrade(db.alembic(), "head")


def _add_account(conn: sa.Connection, username: str = "ansh") -> uuid.UUID:
    return conn.execute(sa.text(
        "INSERT INTO auth.accounts (username, password_hash) VALUES (:u, 'scrypt$x') "
        "RETURNING id"), {"u": username}).scalar_one()


def _add_login(conn: sa.Connection, user_id: uuid.UUID) -> None:
    conn.execute(sa.text(
        "INSERT INTO auth.logins (token_hash, user_id, expires_at) VALUES (:t, :u, :e)"),
        {"t": secrets.token_hex(32), "u": user_id,
         "e": datetime.now(UTC) + timedelta(days=30)})


def _add_profile(conn: sa.Connection, user_id: uuid.UUID) -> None:
    conn.execute(sa.text(
        "INSERT INTO profile.profiles (user_id, prefs_encrypted, key_version) "
        "VALUES (:u, :p, 'v1')"), {"u": user_id, "p": b"\x00ciphertext"})


def _count(conn: sa.Connection, table: str) -> int:
    return conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()


def test_upgrade_creates_the_tables_in_their_schemas(db):
    upgrade(db)
    inspector = sa.inspect(db.engine())
    assert sorted(inspector.get_table_names(schema="auth")) == ["accounts", "logins"]
    assert inspector.get_table_names(schema="profile") == ["profiles"]
    with db.engine().connect() as conn:
        assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() \
            == "0001"


def test_each_user_works_in_its_own_schema(db):
    upgrade(db)
    with db.engine("auth").begin() as conn:
        user_id = _add_account(conn)
        _add_login(conn, user_id)
        assert _count(conn, "auth.logins") == 1
    # The profile user can add a profile for an account it can't see
    with db.engine("profile").begin() as conn:
        _add_profile(conn, user_id)
        assert _count(conn, "profile.profiles") == 1


@pytest.mark.parametrize(("role", "table"), [("auth", "profile.profiles"),
                                             ("profile", "auth.accounts"),
                                             ("profile", "auth.logins")])
def test_neither_user_can_read_the_other_schema(db, role, table):
    upgrade(db)
    with db.engine(role).connect() as conn, pytest.raises(ProgrammingError,
                                                          match="permission denied"):
        conn.execute(sa.text(f"SELECT * FROM {table}"))


def test_neither_user_can_change_the_tables_shape(db):
    upgrade(db)
    with db.engine("auth").connect() as conn, pytest.raises(ProgrammingError,
                                                            match="must be owner"):
        conn.execute(sa.text("ALTER TABLE auth.accounts ADD COLUMN x int"))


def test_deleting_an_account_deletes_its_logins_and_profile(db):
    upgrade(db)
    with db.engine("auth").begin() as conn:
        user_id = _add_account(conn)
        _add_login(conn, user_id)
    with db.engine("profile").begin() as conn:
        _add_profile(conn, user_id)
    with db.engine("auth").begin() as conn:
        conn.execute(sa.text("DELETE FROM auth.accounts WHERE id = :u"), {"u": user_id})
        assert _count(conn, "auth.logins") == 0
    with db.engine("profile").connect() as conn:
        assert _count(conn, "profile.profiles") == 0


def test_a_profile_needs_an_account(db):
    upgrade(db)
    with db.engine("profile").begin() as conn, pytest.raises(IntegrityError):
        _add_profile(conn, uuid.uuid4())


@pytest.mark.parametrize("second", ["ansh", "Ansh"])
def test_usernames_are_unique_and_lower_case(db, second):
    upgrade(db)
    with db.engine("auth").begin() as conn:
        _add_account(conn, "ansh")
    with db.engine("auth").begin() as conn, pytest.raises(IntegrityError):
        _add_account(conn, second)


def test_a_new_user_needs_a_password(db, monkeypatch):
    monkeypatch.delenv("PANTRYPATH_DB_PROFILE_PASSWORD")
    with pytest.raises(RuntimeError, match="PANTRYPATH_DB_PROFILE_PASSWORD"):
        upgrade(db)
    # Nothing was left half done
    assert sa.inspect(db.engine()).get_schema_names().count("auth") == 0


def test_existing_users_need_no_password_again(db, monkeypatch):
    """A later deploy (or a second database) finds the users already there."""
    upgrade(db)
    command.downgrade(db.alembic(), "base")
    with admin_engine().connect() as conn:   # put the users back, as if another db had them
        conn.execute(sa.text(f'CREATE ROLE "{db.auth_role}" LOGIN PASSWORD '
                             f"'{db.auth_password}'"))
        conn.execute(sa.text(f'CREATE ROLE "{db.profile_role}" LOGIN PASSWORD '
                             f"'{db.profile_password}'"))
    monkeypatch.delenv("PANTRYPATH_DB_AUTH_PASSWORD")
    monkeypatch.delenv("PANTRYPATH_DB_PROFILE_PASSWORD")
    upgrade(db)
    with db.engine("auth").begin() as conn:
        _add_account(conn)


def test_downgrade_removes_everything(db):
    upgrade(db)
    command.downgrade(db.alembic(), "base")
    schemas = sa.inspect(db.engine()).get_schema_names()
    assert "auth" not in schemas and "profile" not in schemas
    with admin_engine().connect() as conn:
        roles = conn.execute(sa.text("SELECT rolname FROM pg_roles WHERE rolname IN "
                                     "(:a, :p)"), {"a": db.auth_role,
                                                   "p": db.profile_role}).all()
    assert roles == []
