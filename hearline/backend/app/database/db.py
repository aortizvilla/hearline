"""Database engine, session dependency and table creation."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from app.config import get_settings


def make_engine(url: str) -> Engine:
    """Create an engine; SQLite gets WAL + a busy timeout so worker threads can share it."""
    if not url.startswith("sqlite"):  # Postgres: one pool per process, sized for its threads
        return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20)
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_conn: Any, _: Any) -> None:
        cur = dbapi_conn.cursor()
        if ":memory:" not in url and url != "sqlite://":
            cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return eng


engine = make_engine(get_settings().database_url)


def create_db_and_tables(bind: Engine | None = None) -> None:
    """Bring the database to the latest schema (called on startup): creates a new database
    from the models, or applies pending migrations (see app/database/migrate.py)."""
    from app.database.migrate import migrate

    migrate(bind or engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a database session."""
    with Session(engine) as session:
        yield session
