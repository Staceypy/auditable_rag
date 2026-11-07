"""RAG answer with citations: retrieve chunks, prompt the pinned model, map citations back.

The prompt numbers the retrieved chunks [1], [2], ... and asks the model to
cite them. parse_citations() maps those markers back to document, version,
page and chunk.

The exact messages sent to the model are never stored, because they can be
rebuilt: the prompt version (config.PROMPTS), the question (its blob) and the
chunk texts (by chunk id, checked against content_hash) are all recorded, and
build_messages() is deterministic. A test checks this round trip.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

import psycopg

from app import config
from app.embeddings import Embedder
from app.retrieve import RetrievedChunk, retrieve

# Shown instead of calling the model when retrieval finds nothing at all.
NO_CONTEXT_ANSWER = "No documents are available to answer this question."

# One or more numbers in square brackets: [1], [1, 2], [1][3]
_CITATION = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


# ---------------------------------------------------------------------------
# Chat client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatResult:
    text: str
    model: str | None               # the model name the provider says it used
    system_fingerprint: str | None  # provider's backend configuration id
    finish_reason: str | None       # "length" means the answer was cut off


class ChatClient(Protocol):
    def complete(self, model: str, messages: list[dict], **params) -> ChatResult: ...


class OpenAIChat:
    def __init__(self, client=None):
        self._client = client

    def complete(self, model: str, messages: list[dict], **params) -> ChatResult:
        if self._client is None:
            from openai import OpenAI  # reads OPENAI_API_KEY

            self._client = OpenAI()
        response = self._client.chat.completions.create(model=model, messages=messages, **params)
        choice = response.choices[0]
        return ChatResult(
            text=choice.message.content or "",
            model=response.model,
            system_fingerprint=response.system_fingerprint,
            finish_reason=choice.finish_reason,
        )


# ---------------------------------------------------------------------------
# Prompt and citations (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    marker: int  # the number the model wrote, e.g. 2 for "[2]"
    chunk: RetrievedChunk


def build_context(chunks: list[RetrievedChunk]) -> str:
    return "\n\n".join(
        f"[{n}] {c.title} (version {c.document_version}, page {c.page})\n{c.content}"
        for n, c in enumerate(chunks, start=1)
    )


def build_messages(prompt: config.Prompt, chunks: list[RetrievedChunk], question: str) -> list[dict]:
    return [
        {"role": "system", "content": prompt.system},
        {"role": "user", "content": prompt.user_template.format(
            context=build_context(chunks), question=question)},
    ]


def parse_citations(text: str, chunks: list[RetrievedChunk]) -> tuple[list[Citation], list[int]]:
    """Map [n] markers in the answer to the chunks they refer to.

    Returns (citations in order of first appearance, invalid markers). A
    marker with no matching chunk, e.g. [7] when only 4 chunks were given,
    is a model error. It's reported, not silently fixed: the answer text is
    kept exactly as the user saw it.
    """
    citations: list[Citation] = []
    invalid: list[int] = []
    seen: set[int] = set()
    for group in _CITATION.findall(text):
        for marker in (int(n) for n in group.split(",")):
            if marker in seen:
                continue
            seen.add(marker)
            if 1 <= marker <= len(chunks):
                citations.append(Citation(marker, chunks[marker - 1]))
            else:
                invalid.append(marker)
    return citations, invalid


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    text: str
    citations: list[Citation]
    invalid_markers: list[int]
    chunks: list[RetrievedChunk]  # everything retrieved, cited or not
    prompt: config.Prompt
    embedding_model: str
    model_requested: str
    model_params: dict = field(default_factory=dict)
    top_k: int = config.TOP_K
    result: ChatResult | None = None  # None if the model wasn't called

    def audit_fields(self) -> dict:
        """Model, prompt and retrieval details for the answer's audit event.

        Identities and hashes only, no document text. The question and answer
        text go in audit_blobs and are referenced by hash (app/api.py).
        """
        cited = {c.chunk.chunk_id for c in self.citations}
        return {
            "model": {
                "requested": self.model_requested,
                "reported": self.result.model if self.result else None,
                "system_fingerprint": self.result.system_fingerprint if self.result else None,
                "finish_reason": self.result.finish_reason if self.result else None,
                "params": self.model_params,
                "called": self.result is not None,
            },
            "embedding_model": self.embedding_model,
            "prompt": {"id": self.prompt.id, "version": self.prompt.version,
                       "hash": self.prompt.hash},
            "retrieval": {
                "top_k": self.top_k,
                "chunks": [{**c.audit_record(), "cited": c.chunk_id in cited}
                           for c in self.chunks],
            },
            "invalid_citation_markers": self.invalid_markers,
        }

    def citations_for_user(self) -> list[dict]:
        return [
            {"marker": c.marker, "chunk_id": c.chunk.chunk_id, "document_id": c.chunk.document_id,
             "title": c.chunk.title, "document_version": c.chunk.document_version,
             "page": c.chunk.page}
            for c in self.citations
        ]


def generate_answer(
    conn: psycopg.Connection,
    embedder: Embedder,
    chat: ChatClient,
    question: str,
    prompt: config.Prompt | None = None,
) -> Answer:
    prompt = prompt or config.get_prompt()
    chunks = retrieve(conn, embedder, question, top_k=config.TOP_K)
    common = dict(prompt=prompt, embedding_model=embedder.model, top_k=config.TOP_K,
                  model_requested=config.CHAT_MODEL, model_params=dict(config.CHAT_PARAMS))

    if not chunks:
        return Answer(text=NO_CONTEXT_ANSWER, citations=[], invalid_markers=[], chunks=[], **common)

    result = chat.complete(config.CHAT_MODEL, build_messages(prompt, chunks, question),
                           **config.CHAT_PARAMS)
    citations, invalid = parse_citations(result.text, chunks)
    return Answer(text=result.text, citations=citations, invalid_markers=invalid,
                  chunks=chunks, result=result, **common)
