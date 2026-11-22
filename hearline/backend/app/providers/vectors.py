"""Vector similarity behind an interface so pgvector can replace the in-Python version."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

import numpy as np
from sqlmodel import Session, col, select

from app.database.models import Story, Topic


def cosine_top_k(
    query: list[float], items: list[tuple[int, list[float]]], k: int
) -> list[tuple[int, float]]:
    """Return (id, similarity) of the k most similar unit vectors."""
    items = [(i, v) for i, v in items if v]
    if not items or not query:
        return []
    ids = [i for i, _ in items]
    sims = np.asarray([v for _, v in items]) @ np.asarray(query)
    order = np.argsort(-sims)[:k]
    return [(ids[j], float(sims[j])) for j in order]


class VectorStore(ABC):
    """Nearest-neighbour queries over topic and story embeddings."""

    @abstractmethod
    def nearest_topics(
        self, session: Session, vector: list[float], k: int
    ) -> list[tuple[Topic, float]]: ...

    @abstractmethod
    def nearest_stories(
        self,
        session: Session,
        vector: list[float],
        k: int,
        topic_id: int | None = None,
        since: datetime | None = None,
    ) -> list[tuple[Story, float]]: ...


class PythonVectorStore(VectorStore):
    """Loads candidate embeddings from the DB and ranks them with numpy."""

    def nearest_topics(
        self, session: Session, vector: list[float], k: int
    ) -> list[tuple[Topic, float]]:
        topics = {t.id: t for t in session.exec(select(Topic)) if t.embedding}
        ranked = cosine_top_k(vector, [(i, t.embedding or []) for i, t in topics.items()], k)
        return [(topics[i], s) for i, s in ranked]

    def nearest_stories(
        self,
        session: Session,
        vector: list[float],
        k: int,
        topic_id: int | None = None,
        since: datetime | None = None,
    ) -> list[tuple[Story, float]]:
        q = select(Story)
        if topic_id is not None:
            q = q.where(Story.topic_id == topic_id)
        if since is not None:
            q = q.where(col(Story.first_seen) >= since)
        stories = {s.id: s for s in session.exec(q) if s.embedding}
        ranked = cosine_top_k(vector, [(i, s.embedding or []) for i, s in stories.items()], k)
        return [(stories[i], s) for i, s in ranked]
