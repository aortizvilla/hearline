"""FastAPI application entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from app.api import admin, feed, webhooks
from app.api.routes import router
from app.config import get_settings
from app.database.db import create_db_and_tables, engine
from app.pipeline.step1_topics import seed_subtopics, seed_topics
from app.providers import default_embedder
from app.providers.voices import seed_voices


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Bring the database up to date and load the topic and voice catalogs on startup."""
    create_db_and_tables()
    with Session(engine) as session:
        seed_topics(session, default_embedder())  # idempotent; embeds only new topics
        seed_voices(session)  # config/voices.yaml -> dim_voice
        seed_subtopics(session, default_embedder())  # config/subtopics.yaml -> dim_subtopic
    yield


app = FastAPI(title="Hearline", lifespan=lifespan)
settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(admin.router)
app.include_router(feed.router)  # private podcast feed
app.include_router(webhooks.router)  # Clerk: name/email sync and account erasure

if settings.audio_storage in ("", "local"):  # dev: serve files directly; S3 uses signed links
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/audio", StaticFiles(directory=settings.audio_dir), name="audio")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}
