"""Test fixtures: in-memory databases and offline providers (no network, no API keys)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings, pipeline_cfg, plans_cfg
from app.database.db import get_session
from app.main import app
from app.pipeline.step1_topics import seed_subtopics, seed_topics
from app.providers import Providers, build_providers
from app.providers.embeddings import HashingEmbeddings
from app.providers.mock_llm import MockLLM
from app.providers.tts import MockTTSProvider
from app.providers.voices import seed_voices
from app.tracking import metrics


@pytest.fixture
def engine():  # type: ignore[no-untyped-def]
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine) -> Iterator[Session]:  # type: ignore[no-untyped-def]
    with Session(engine) as session:
        yield session


@pytest.fixture
def client(session: Session, providers: Providers,
           monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    seed_topics(session, HashingEmbeddings(256))  # the app does this at startup
    seed_subtopics(session, HashingEmbeddings(256))
    seed_voices(session)
    # Step 1 builds providers from .env when a phrase needs a model: use the offline mocks,
    # so API tests never call a real (paid) API.
    monkeypatch.setattr("app.pipeline.step1_topics.build_providers", lambda: providers)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Audio goes to a temp dir; config mutations and counters are undone after each test."""
    monkeypatch.setattr(get_settings(), "audio_dir", tmp_path / "audio")
    # feature tests sign users up on the plan without limits; tests/test_plans.py covers Free
    monkeypatch.setattr(plans_cfg(), "default", "pro")
    # research's web lookups never leave the machine in tests: links count as alive and
    # Wikipedia has nothing (tests that need them patch these again)
    monkeypatch.setattr("app.providers.web.article_link_ok", lambda url: True)
    # one worker thread in tests: the in-memory test database is a single shared connection
    monkeypatch.setattr(pipeline_cfg().scheduler, "worker_threads", 1)
    monkeypatch.setattr("app.providers.web.wikipedia_background", lambda term, language="en": None)
    monkeypatch.setattr(get_settings(), "archive_dir", tmp_path / "archive")
    snapshot = pipeline_cfg().model_copy(deep=True)
    metrics.reset()
    yield
    cfg = pipeline_cfg()
    for name in type(cfg).model_fields:
        setattr(cfg, name, getattr(snapshot, name))


@pytest.fixture
def llm() -> MockLLM:
    return MockLLM()


@pytest.fixture
def providers(llm: MockLLM) -> Providers:
    return build_providers(mock=True, llm=llm, tts=MockTTSProvider(8000),
                           embedder=HashingEmbeddings(256))
