"""EmbeddingProvider interface with local implementations (swappable via config)."""

from __future__ import annotations

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

import numpy as np

log = logging.getLogger(__name__)


class EmbeddingProvider(ABC):
    """Turns texts into unit-length vectors."""

    name: str = "base"

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one L2-normalized vector per text."""

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


def _normalize(m: np.ndarray) -> list[list[float]]:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype(float).round(6).tolist()


class HashingEmbeddings(EmbeddingProvider):
    """Deterministic bag of words + character trigrams hashed into a fixed dimension.

    No model download; good for near-duplicate detection and tests, weak on synonyms.
    """

    name = "hashing"

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def _features(self, text: str) -> list[str]:
        words = re.findall(r"\w+", text.lower())
        grams = [f"#{w[i:i + 3]}" for w in words for i in range(max(1, len(w) - 2))]
        return words + grams

    def embed(self, texts: list[str]) -> list[list[float]]:
        m = np.zeros((len(texts), self.dim))
        for row, text in enumerate(texts):
            for feat in self._features(text):
                h = int.from_bytes(hashlib.md5(feat.encode()).digest()[:4], "little")
                m[row, h % self.dim] += 1.0 if not feat.startswith("#") else 0.5
        return _normalize(m)


class SentenceTransformerEmbeddings(EmbeddingProvider):
    """Local sentence-transformers model; falls back to the same model on ONNX (fastembed)."""

    name = "sentence-transformers"

    def __init__(self, model: str) -> None:
        self.model_name = model
        self._model: Any = None
        self._backend = ""

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer

            self._model, self._backend = SentenceTransformer(self.model_name), "torch"
        except ImportError:
            from fastembed import TextEmbedding  # same weights, ONNX runtime

            self._model, self._backend = TextEmbedding(self.model_name), "onnx"
            log.info("sentence-transformers unavailable; using fastembed for %s", self.model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._load()
        if self._backend == "torch":
            m = np.asarray(self._model.encode(texts))
        else:
            m = np.asarray(list(self._model.embed(texts)))
        return _normalize(m)


def make_embedder(provider: str, model: str, hashing_dim: int = 256) -> EmbeddingProvider:
    if provider == "hashing":
        return HashingEmbeddings(hashing_dim)
    if provider == "sentence-transformers":
        try:
            import fastembed  # noqa: F401
        except ImportError:
            try:
                import sentence_transformers  # noqa: F401
            except ImportError:
                log.warning("No local embedding library installed; using hashing embeddings.")
                return HashingEmbeddings(hashing_dim)
        return SentenceTransformerEmbeddings(model)
    raise ValueError(f"Unknown embedding provider: {provider}")
