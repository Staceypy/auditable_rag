"""Answer generation: prompt building, citation mapping, audit fields, OpenAI adapters."""
import json
from types import SimpleNamespace

import pytest

from app import config
from app.answer import (
    NO_CONTEXT_ANSWER, OpenAIChat, build_context, build_messages, generate_answer,
    parse_citations,
)
from app.audit import sha256_text
from app.embeddings import OpenAIEmbedder
from app.ingest import SAMPLE_DOCS, ingest_directory
from app.retrieve import RetrievedChunk
from tests.fakes import FakeChat, FakeEmbedder

QUESTION = "How many days notice is needed for a share transfer under the right of first refusal?"


def chunk(n: int, page: int = 1) -> RetrievedChunk:
    return RetrievedChunk(chunk_id=100 + n, document_id=1, title="Doc", document_version=2,
                          page=page, content=f"text {n}", content_hash=sha256_text(f"text {n}"),
                          score=0.5)


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture
def corpus(app_conn, embedder):
    ingest_directory(app_conn, embedder, SAMPLE_DOCS)


# --- citations ---------------------------------------------------------------------


@pytest.mark.parametrize("text, markers, invalid", [
    ("Yes [1].", [1], []),
    ("A [2][3] and B [1].", [2, 3, 1], []),
    ("A [1, 3].", [1, 3], []),
    ("Repeated [2] and [2].", [2], []),
    ("Bad [7] and [0], good [1].", [1], [7, 0]),
    ("No citations here.", [], []),
    ("Not a citation: [a], [1.5]", [], []),
])
def test_parse_citations(text, markers, invalid):
    chunks = [chunk(1), chunk(2), chunk(3)]
    citations, bad = parse_citations(text, chunks)
    assert [c.marker for c in citations] == markers
    assert [c.chunk.chunk_id for c in citations] == [100 + m for m in markers]
    assert bad == invalid


def test_context_numbers_chunks_with_title_version_and_page():
    context = build_context([chunk(1, page=3), chunk(2)])
    assert context == "[1] Doc (version 2, page 3)\ntext 1\n\n[2] Doc (version 2, page 1)\ntext 2"


# --- generate_answer -------------------------------------------------------------


def test_answer_uses_pinned_model_params_and_prompt(app_conn, embedder, corpus):
    chat = FakeChat()
    answer = generate_answer(app_conn, embedder, chat, QUESTION)

    [call] = chat.calls
    assert call["model"] == config.CHAT_MODEL
    assert call["params"] == config.CHAT_PARAMS
    system, user = call["messages"]
    assert system == {"role": "system", "content": config.get_prompt().system}
    assert user["content"].endswith(f"Question: {QUESTION}")
    assert len(answer.chunks) == config.TOP_K


def test_citation_maps_back_to_document_page_and_chunk(app_conn, embedder, corpus):
    answer = generate_answer(app_conn, embedder, FakeChat(), QUESTION)
    [citation] = answer.citations_for_user()
    top = answer.chunks[0]
    assert citation == {"marker": 1, "chunk_id": top.chunk_id, "document_id": top.document_id,
                        "title": top.title, "document_version": 2, "page": 2}
    assert "45 days" in top.content


def test_audit_fields_record_model_prompt_and_chunks_without_text(app_conn, embedder, corpus):
    answer = generate_answer(app_conn, embedder, FakeChat(reply="See [2] and [9]."), QUESTION)
    fields = answer.audit_fields()

    assert fields["model"] == {
        "requested": config.CHAT_MODEL, "reported": "gpt-4o-mini-2024-07-18",
        "system_fingerprint": "fp_fake", "finish_reason": "stop",
        "params": config.CHAT_PARAMS, "called": True,
    }
    assert fields["embedding_model"] == embedder.model
    assert fields["prompt"] == {"id": "rag_answer", "version": "v1",
                                "hash": config.get_prompt().hash}
    assert fields["retrieval"]["top_k"] == config.TOP_K
    assert [c["cited"] for c in fields["retrieval"]["chunks"]] == [False, True, False, False]
    assert fields["invalid_citation_markers"] == [9]

    # No document text leaks into the event; only hashes and ids.
    serialized = json.dumps(fields)
    assert all(c.content not in serialized for c in answer.chunks)


def test_sent_messages_can_be_rebuilt_from_audit_fields(app_conn, embedder, corpus):
    """Auditability: the stored ids, hashes and prompt version are enough to
    reconstruct exactly what the model was sent."""
    chat = FakeChat()
    answer = generate_answer(app_conn, embedder, chat, QUESTION)
    fields = json.loads(json.dumps(answer.audit_fields()))  # as if read back from the event

    rebuilt_chunks = []
    for record in fields["retrieval"]["chunks"]:
        row = app_conn.execute(
            "SELECT c.content, c.page, c.document_version, d.title FROM chunks c "
            "JOIN documents d ON d.id = c.document_id WHERE c.id = %s", (record["chunk_id"],)
        ).fetchone()
        assert sha256_text(row["content"]) == record["content_hash"]  # text is unchanged
        rebuilt_chunks.append(RetrievedChunk(
            chunk_id=record["chunk_id"], document_id=record["document_id"], title=row["title"],
            document_version=row["document_version"], page=row["page"], content=row["content"],
            content_hash=record["content_hash"], score=record["score"]))
    prompt = config.get_prompt(fields["prompt"]["id"], fields["prompt"]["version"])
    assert prompt.hash == fields["prompt"]["hash"]

    assert build_messages(prompt, rebuilt_chunks, QUESTION) == chat.calls[0]["messages"]


def test_no_documents_means_no_model_call(app_conn, embedder):
    chat = FakeChat()
    answer = generate_answer(app_conn, embedder, chat, QUESTION)
    assert answer.text == NO_CONTEXT_ANSWER
    assert chat.calls == []
    fields = answer.audit_fields()
    assert fields["model"]["called"] is False and fields["retrieval"]["chunks"] == []


# --- OpenAI adapters (stub clients, no network) ------------------------------------


def test_openai_chat_adapter_passes_params_and_reads_response():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            model="gpt-4o-mini-2024-07-18", system_fingerprint="fp_123",
            choices=[SimpleNamespace(message=SimpleNamespace(content="Answer [1]."),
                                     finish_reason="stop")],
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = OpenAIChat(client).complete("m", [{"role": "user", "content": "q"}], temperature=0)
    assert calls == [{"model": "m", "messages": [{"role": "user", "content": "q"}],
                      "temperature": 0}]
    assert (result.text, result.model, result.system_fingerprint, result.finish_reason) == \
        ("Answer [1].", "gpt-4o-mini-2024-07-18", "fp_123", "stop")


def test_openai_embedder_batches_and_pins_model():
    calls = []

    def create(model, input, dimensions):
        calls.append((model, len(input), dimensions))
        return SimpleNamespace(data=[SimpleNamespace(embedding=[0.0]) for _ in input])

    embedder = OpenAIEmbedder(SimpleNamespace(embeddings=SimpleNamespace(create=create)))
    vectors = embedder.embed([f"t{i}" for i in range(250)])
    assert len(vectors) == 250
    assert calls == [(config.EMBEDDING_MODEL, 100, 1536), (config.EMBEDDING_MODEL, 100, 1536),
                     (config.EMBEDDING_MODEL, 50, 1536)]
