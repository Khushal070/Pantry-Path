"""How Alembic connects: as the database owner, from PANTRYPATH_DB_OWNER_URL.

The owner creates the schemas, the tables and the two app users (pp_auth, pp_profile); the
app itself never connects as the owner. Tests pass their own URL in
config.attributes["db_url"] instead of the environment variable.

Only online migrations: the first migration sets the app users' passwords, and offline
mode (alembic upgrade --sql) would print them.
"""

import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool

from app.settings import REPO_ROOT

config = context.config
if config.config_file_name and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)


def owner_url() -> str:
    if config.attributes.get("db_url"):
        return config.attributes["db_url"]
    load_dotenv(os.environ.get("PANTRYPATH_ENV_FILE") or REPO_ROOT / ".env", override=False)
    url = os.environ.get("PANTRYPATH_DB_OWNER_URL")
    if not url:
        raise SystemExit("Set PANTRYPATH_DB_OWNER_URL (see .env.example) to run migrations.")
    return url


if context.is_offline_mode():
    raise SystemExit("Offline migrations (--sql) aren't supported: they would print the "
                     "app users' passwords. Run them against the database instead.")

engine = create_engine(owner_url(), poolclass=pool.NullPool)
with engine.connect() as connection:
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
