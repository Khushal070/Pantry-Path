"""Accounts, logins and profiles, in two schemas with one database user each.

    auth.accounts       username, password hash (scrypt, app/auth.py)
    auth.logins         one logged-in browser: the token's SHA-256 and when it expires
    profile.profiles    the food preferences, encrypted by the app (bytea), and the key
                        version that encrypted them

pp_auth can use only the auth schema and pp_profile only the profile schema, so neither
can read the other's tables. Deleting an account deletes its logins and profile
(ON DELETE CASCADE); Postgres runs those deletes and the foreign-key checks as the tables'
owner, so neither user needs rights on the other schema for them.

The two users are created here with SQL, not with gcloud or the Cloud SQL console: users
made there join cloudsqlsuperuser, which can read every table. Their names and passwords
come from the environment:

    PANTRYPATH_DB_AUTH_ROLE      (default pp_auth)     PANTRYPATH_DB_AUTH_PASSWORD
    PANTRYPATH_DB_PROFILE_ROLE   (default pp_profile)  PANTRYPATH_DB_PROFILE_PASSWORD

A password is needed only when its user doesn't exist yet; when one is set for a user that
exists, that user's password is changed to it.

Revision ID: 0001
Revises:
Created: 2026-11-27
"""

import os
import re

import sqlalchemy as sa
from alembic import op
from psycopg import sql
from sqlalchemy.dialects.postgresql import BYTEA, UUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
# schema -> (role variable, default role name, password variable, its tables)
SCHEMAS = {
    "auth": ("PANTRYPATH_DB_AUTH_ROLE", "pp_auth", "PANTRYPATH_DB_AUTH_PASSWORD",
             ("accounts", "logins")),
    "profile": ("PANTRYPATH_DB_PROFILE_ROLE", "pp_profile", "PANTRYPATH_DB_PROFILE_PASSWORD",
                ("profiles",)),
}


def _role(schema: str) -> str:
    role_var, default, _, _ = SCHEMAS[schema]
    name = os.environ.get(role_var) or default
    if not ROLE_RE.match(name):
        raise ValueError(f"{role_var}={name!r} isn't a valid database user name")
    return name


def _run(statement: sql.Composable) -> None:
    """A statement with quoted names and values, in the migration's transaction.
    (DDL such as CREATE ROLE ... PASSWORD can't take bind parameters.)"""
    op.get_bind().connection.driver_connection.execute(statement)


def _create_or_update_role(schema: str) -> str:
    _, _, password_var, _ = SCHEMAS[schema]
    name, password = _role(schema), os.environ.get(password_var)
    exists = op.get_bind().execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": name}).scalar()
    if not exists and not password:
        raise RuntimeError(f"The database user {name} doesn't exist yet: set {password_var} "
                           "so the migration can create it.")
    verb = "ALTER" if exists else "CREATE"
    if password:
        _run(sql.SQL(verb + " ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(name), sql.Literal(password)))
    return name


def _timestamps() -> list[sa.Column]:
    return [sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                      server_default=sa.func.now())]


def upgrade() -> None:
    op.execute("CREATE SCHEMA auth")
    op.execute("CREATE SCHEMA profile")

    op.create_table(
        "accounts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("username", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("username", name="accounts_username_key"),
        # The app lower-cases usernames; this keeps "Ansh" and "ansh" from both existing
        sa.CheckConstraint("username = lower(username)", name="accounts_username_lower"),
        schema="auth",
    )
    op.create_table(
        "logins",
        sa.Column("token_hash", sa.Text, primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("auth.accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="logins_token_hash_sha256"),
        schema="auth",
    )
    op.create_index("logins_user_id_idx", "logins", ["user_id"], schema="auth")
    op.create_index("logins_expires_at_idx", "logins", ["expires_at"], schema="auth")

    op.create_table(
        "profiles",
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("auth.accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("prefs_encrypted", BYTEA, nullable=False),
        sa.Column("key_version", sa.Text, nullable=False),
        *_timestamps(),
        schema="profile",
    )

    for schema, (_, _, _, tables) in SCHEMAS.items():
        role = _create_or_update_role(schema)
        _run(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
        _run(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(schema), sql.Identifier(role)))
        _run(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
            sql.SQL(", ").join(sql.Identifier(schema, t) for t in tables),
            sql.Identifier(role)))


def downgrade() -> None:
    """Deletes every account and profile, then the two users."""
    op.execute("DROP SCHEMA profile CASCADE")
    op.execute("DROP SCHEMA auth CASCADE")
    for schema in SCHEMAS:
        _run(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(_role(schema))))
