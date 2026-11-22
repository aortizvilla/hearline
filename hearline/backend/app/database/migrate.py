"""Bring any database to the latest schema with the Alembic migrations in migrations/.

    new, empty database          -> create every table from the models, mark it current
    created before migrations    -> mark it as the baseline (0001), then apply the rest
    already migrated             -> apply what's new

Runs on startup (API, worker, CLI). In production run `python -m app.cli migrate` once per
deploy, before the new containers start, so several processes never migrate at the same time.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

log = logging.getLogger(__name__)
BASELINE = "0001"


def _config(connection: object) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.attributes["connection"] = connection
    return cfg


def migrate(engine: Engine) -> str:
    """Upgrade `engine`'s database to the latest revision. Returns what was done."""
    from app.database import models  # noqa: F401  (registers tables on SQLModel.metadata)

    with engine.begin() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
        tables = set(inspect(conn).get_table_names()) - {"alembic_version"}
        cfg = _config(conn)
        if current is None and not tables:
            SQLModel.metadata.create_all(conn)
            command.stamp(cfg, "head")
            action = "created"
        else:
            if current is None:  # made by create_all before migrations existed
                command.stamp(cfg, BASELINE)
            command.upgrade(cfg, "head")
            action = "upgraded"
        new = MigrationContext.configure(conn).get_current_revision()
    log.info("Database %s (revision %s)", action, new)
    return f"{action} to {new}"
