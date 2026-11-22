"""Alembic environment: migrates the database in DATABASE_URL to app.database.models.

Run through `python -m app.cli migrate` (or automatically on startup, see
app/database/migrate.py). Batch mode is on so the same migrations work on SQLite (dev) and
Postgres (production).
"""

from alembic import context
from sqlalchemy.engine import Connection
from sqlmodel import SQLModel

from app.config import get_settings
from app.database import models  # noqa: F401  (registers every table on SQLModel.metadata)

config = context.config
target_metadata = SQLModel.metadata


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata,
                      render_as_batch=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")  # passed in by app.database.migrate
    if connection is not None:
        _run(connection)
        return
    from app.database.db import make_engine

    with make_engine(get_settings().database_url).connect() as conn:
        _run(conn)


if context.is_offline_mode():
    raise SystemExit("Offline (SQL script) migrations are not supported; run them online.")
run_migrations_online()
