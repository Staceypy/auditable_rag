"""Embedding client, behind a small interface so tests can use a fake."""
from __future__ import annotations

from typing import Protocol

from app import config


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    """Embeds texts with the pinned OpenAI embedding model."""

    model = config.EMBEDDING_MODEL
    batch_size = 100

    def __init__(self, client=None):
        self._client = client

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._client is None:
            from openai import OpenAI  # reads OPENAI_API_KEY

            self._client = OpenAI()
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            response = self._client.embeddings.create(
                model=self.model,
                input=texts[start:start + self.batch_size],
                dimensions=config.EMBEDDING_DIMENSIONS,
            )
            vectors.extend(item.embedding for item in response.data)
        return vectors


def to_vector_literal(vector: list[float]) -> str:
    """Format a vector as pgvector's text input, e.g. '[0.1,0.2]'.

    Sent as text and cast with ::vector in SQL, which avoids needing numpy.
    """
    if len(vector) != config.EMBEDDING_DIMENSIONS:
        raise ValueError(f"expected {config.EMBEDDING_DIMENSIONS} dimensions, got {len(vector)}")
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"
