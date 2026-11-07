"""Test doubles for the OpenAI clients, so tests need no API key."""
import hashlib
import math
import re

from app import config


class FakeEmbedder:
    """Deterministic bag-of-words embedding.

    Each word is hashed to one of the 1536 dimensions, so texts that share
    words point in similar directions. That's enough for retrieval tests to
    behave like real search, without any network call.
    """

    model = "fake-embedding"

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(t) for t in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        vector = [0.0] * config.EMBEDDING_DIMENSIONS
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            index = int(hashlib.sha256(word.encode()).hexdigest(), 16) % len(vector)
            vector[index] += 1.0
        norm = math.sqrt(sum(x * x for x in vector))
        if norm == 0:  # no words at all: any fixed direction (zero vectors break cosine)
            vector[0], norm = 1.0, 1.0
        return [x / norm for x in vector]
