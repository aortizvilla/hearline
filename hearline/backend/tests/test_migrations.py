"""Migrations: they rebuild exactly the models' schema, and old databases are brought up to it."""

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.database.migrate import _config, migrate


def _memory_engine():  # type: ignore[no-untyped-def]
    return create_engine("sqlite://", connect_args={"check_same_thread": False},
                         poolclass=StaticPool)


def test_migrations_from_empty_match_the_models() -> None:
    from alembic import command

    eng = _memory_engine()
    with eng.begin() as conn:
        command.upgrade(_config(conn), "head")  # every migration, step by step
        diff = compare_metadata(MigrationContext.configure(conn), SQLModel.metadata)
    assert diff == [], "models changed without a migration: run alembic revision --autogenerate"


def test_new_database_is_created_and_stamped() -> None:
    eng = _memory_engine()
    assert migrate(eng).startswith("created")
    assert migrate(eng).startswith("upgraded")  # second start: nothing to do
    with eng.connect() as conn:
        assert "external_id" in {c["name"] for c in inspect(conn).get_columns("dim_user")}


def test_database_from_before_migrations_is_upgraded() -> None:
    from alembic import command

    eng = _memory_engine()
    with eng.begin() as conn:  # the schema create_all made before migrations existed (0001),
        command.upgrade(_config(conn), "0001")  # with no alembic_version marker
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(text("INSERT INTO dim_user (id, name, email, created_at, last_active_at) "
                          "VALUES (1, 'Listener', '', '2026-11-01', '2026-11-01')"))
    assert migrate(eng).startswith("upgraded")
    with eng.connect() as conn:
        cols = {c["name"] for c in inspect(conn).get_columns("dim_user")}
        assert "external_id" in cols
        assert conn.execute(text("SELECT name FROM dim_user")).scalar() == "Listener"
        assert "dim_subtopic" in inspect(conn).get_table_names()


def test_english_samples_survive_the_per_language_previews_migration() -> None:
    from alembic import command

    eng = _memory_engine()
    with eng.begin() as conn:
        command.upgrade(_config(conn), "0003")
        conn.execute(text("INSERT INTO dim_voice (key, label, style, gender, accent, sort_order, "
                          "active, providers, preview_path, updated_at) VALUES ('aria', 'Aria', "
                          "'', '', '', 0, 1, '{}', 'voices/x/aria.mp3', '2026-11-22')"))
        command.upgrade(_config(conn), "head")
        previews = conn.execute(text("SELECT previews FROM dim_voice")).scalar()
    assert previews in ('{"en": "voices/x/aria.mp3"}', {"en": "voices/x/aria.mp3"})

