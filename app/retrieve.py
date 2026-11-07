"""Vector search over the latest version of each document."""
from __future__ import annotations

from dataclasses import asdict, dataclass

import psycopg

from app import config
from app.embeddings import Embedder, to_vector_literal


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int
    document_id: int
    title: str
    document_version: int
    page: int
    content: str
    content_hash: str
    score: float  # cosine similarity, higher is closer

    def audit_record(self) -> dict:
        """What the answer event stores about this chunk: identity, not text.

        content_hash pins the exact text that was used, even if the document
        gets a new version later.
        """
        record = asdict(self)
        del record["content"], record["title"]
        record["score"] = round(self.score, 6)
        return record


# Only the newest version of each title is searchable; older versions stay
# stored for audit purposes. Exact (sequential) search: the demo corpus is
# tiny, so an approximate index (HNSW/IVFFlat) would add nothing.
_SEARCH_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (title) id, title
    FROM documents
    ORDER BY title, version DESC
)
SELECT c.id AS chunk_id, c.document_id, d.title, c.document_version, c.page,
       c.content, c.content_hash,
       1 - (c.embedding <=> %(query)s::vector) AS score
FROM chunks c
JOIN latest d ON d.id = c.document_id
ORDER BY c.embedding <=> %(query)s::vector, c.id
LIMIT %(top_k)s
"""


def retrieve(
    conn: psycopg.Connection, embedder: Embedder, question: str, top_k: int = config.TOP_K
) -> list[RetrievedChunk]:
    [query_vector] = embedder.embed([question])
    rows = conn.execute(
        _SEARCH_SQL, {"query": to_vector_literal(query_vector), "top_k": top_k}
    ).fetchall()
    return [RetrievedChunk(**row) for row in rows]
