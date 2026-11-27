"""Saved shopping lists: one encrypted value per list, readable only by pp_profile.

    profile.lists    a finished search's checklist (app/lists.py): the dish, the items with
                     their status and tick, the steps. Never Kroger data (product, brand,
                     store); encrypted by the app like the profile.

Deleting an account deletes its lists (ON DELETE CASCADE, run as the table's owner).

Revision ID: 0002
Revises: 0001
Created: 2026-11-27
"""

import os
import re

import sqlalchemy as sa
from alembic import op
from psycopg import sql
from sqlalchemy.dialects.postgresql import BYTEA, UUID

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _profile_role() -> str:
    """The profile user's name, read the same way as in 0001."""
    name = os.environ.get("PANTRYPATH_DB_PROFILE_ROLE") or "pp_profile"
    if not re.match(r"^[a-z_][a-z0-9_]{0,62}$", name):
        raise ValueError(f"PANTRYPATH_DB_PROFILE_ROLE={name!r} isn't a valid database user name")
    return name


def upgrade() -> None:
    op.create_table(
        "lists",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("auth.accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("list_encrypted", BYTEA, nullable=False),
        sa.Column("key_version", sa.Text, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        schema="profile",
    )
    op.create_index("lists_user_created_idx", "lists", ["user_id", sa.text("created_at DESC")],
                    schema="profile")
    op.get_bind().connection.driver_connection.execute(
        sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON profile.lists TO {}").format(
            sql.Identifier(_profile_role())))


def downgrade() -> None:
    op.drop_table("lists", schema="profile")
