"""Smoke tests: health endpoint and table creation on startup."""

from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.database.db import engine
from app.main import app


def test_health() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_tables_created_on_startup() -> None:
    with TestClient(app):
        tables = set(inspect(engine).get_table_names())
    assert {"dim_user", "dim_profile", "fact_episode", "dim_story"} <= tables
