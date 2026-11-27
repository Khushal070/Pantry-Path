"""The two Postgres connections: one per database user, so each part of the app can reach
only its own schema (migrations/versions/0001_accounts_and_profiles.py).

    auth     as pp_auth:     auth.accounts, auth.logins          (auth.py)
    profile  as pp_profile:  profile.profiles, encrypted         (auth.py, crypto.py)

The tables are created by the migrations (alembic upgrade head), never by the app.
"""

import sqlalchemy as sa


class DatabaseConfigError(ValueError):
    """A database URL is missing (the backend won't start)."""


def _engine(url: str | None, name: str) -> sa.Engine:
    if not url:
        raise DatabaseConfigError(
            f"{name} isn't set: the backend keeps accounts and preferences in Postgres "
            "(backend/README.md, Storage).")
    # pre_ping: a connection Cloud SQL closed while idle is replaced instead of failing
    return sa.create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


class Database:
    def __init__(self, auth_url: str | None, profile_url: str | None):
        self.auth = _engine(auth_url, "PANTRYPATH_DB_AUTH_URL")
        self.profile = _engine(profile_url, "PANTRYPATH_DB_PROFILE_URL")

    def check(self) -> None:
        """Connect as both users once, so a wrong URL or password stops the startup."""
        for engine in (self.auth, self.profile):
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))

    def dispose(self) -> None:
        self.auth.dispose()
        self.profile.dispose()
