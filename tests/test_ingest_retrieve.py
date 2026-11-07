"""Config pins, ingest (parse, chunk, version, store) and retrieval."""
from pathlib import Path

import pytest

from app import config
from app.audit import sha256_text
from app.embeddings import to_vector_literal
from app.ingest import (
    SAMPLE_DOCS, IngestError, chunk_pages, ingest_directory, ingest_document, parse_document,
)
from app.retrieve import retrieve
from tests.fakes import FakeEmbedder

DOC = """# Test Agreement

--- page 1 ---
Clause 1. Alpha.

Clause 2. Beta.

--- page 2 ---
Clause 3. Gamma.
"""


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture
def sample_corpus(app_conn, embedder):
    ingest_directory(app_conn, embedder, SAMPLE_DOCS)


# --- config --------------------------------------------------------------------


def test_chat_model_is_a_dated_snapshot():
    assert config.CHAT_MODEL.rsplit("-", 3)[1:] == ["2024", "07", "18"]


def test_prompt_v1_is_never_edited_in_place():
    """If this fails, someone changed a prompt that old answers already reference.

    Add a new version to config.PROMPTS instead, and update ACTIVE_PROMPT_VERSION.
    """
    assert config.get_prompt("rag_answer", "v1").hash == \
        "853746f30c5d4625ea2532d7596e6c5618914b78633933996760640d56ca018a"


def test_unknown_prompt_raises():
    with pytest.raises(KeyError, match="rag_answer@v99"):
        config.get_prompt("rag_answer", "v99")


def test_vector_literal_checks_dimensions():
    with pytest.raises(ValueError):
        to_vector_literal([0.1, 0.2])


# --- parsing and chunking ------------------------------------------------------


def test_parse_document_splits_title_and_pages():
    title, pages = parse_document(DOC)
    assert title == "Test Agreement"
    assert pages == [(1, "Clause 1. Alpha.\n\nClause 2. Beta."), (2, "Clause 3. Gamma.")]


@pytest.mark.parametrize("text, message", [
    ("No title\n--- page 1 ---\nx", "title"),
    ("# T\nstray text\n--- page 1 ---\nx", "before the first"),
    ("# T\nno markers at all", "before the first"),
    ("# T\n", "no '--- page N ---' markers"),
])
def test_parse_document_rejects_malformed_files(text, message):
    with pytest.raises(IngestError, match=message):
        parse_document(text)


def test_chunks_never_cross_pages_and_respect_max_size():
    pages = [(1, "\n\n".join(["a" * 30] * 5)), (2, "b" * 10)]
    chunks = chunk_pages(pages, max_chars=70)
    assert [c.page for c in chunks] == [1, 1, 1, 2]
    assert all(len(c.content) <= 70 for c in chunks)
    # Nothing lost: every paragraph appears exactly once.
    assert sum(c.content.count("a" * 30) for c in chunks) == 5


def test_oversized_paragraph_becomes_its_own_chunk():
    chunks = chunk_pages([(1, "short\n\n" + "x" * 100)], max_chars=50)
    assert [c.content for c in chunks] == ["short", "x" * 100]


# --- storing --------------------------------------------------------------------


def test_ingest_stores_chunks_with_hash_version_and_embedding(app_conn, embedder):
    title, doc_id, status = ingest_document(app_conn, embedder, DOC, version=1)
    assert (title, status) == ("Test Agreement", "added")

    rows = app_conn.execute(
        "SELECT document_version, page, content, content_hash, vector_dims(embedding) AS dims "
        "FROM chunks WHERE document_id = %s ORDER BY id", (doc_id,)
    ).fetchall()
    assert [r["page"] for r in rows] == [1, 2]
    for row in rows:
        assert row["content_hash"] == sha256_text(row["content"])
        assert row["document_version"] == 1
        assert row["dims"] == config.EMBEDDING_DIMENSIONS


def test_reingesting_unchanged_version_is_a_noop(app_conn, embedder):
    ingest_document(app_conn, embedder, DOC, version=1)
    calls = len(embedder.calls)
    _, _, status = ingest_document(app_conn, embedder, DOC, version=1)
    assert status == "unchanged"
    assert len(embedder.calls) == calls  # no embedding cost on re-runs
    assert app_conn.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"] == 2


def test_changed_text_needs_a_new_version(app_conn, embedder):
    ingest_document(app_conn, embedder, DOC, version=1)
    with pytest.raises(IngestError, match="new version number"):
        ingest_document(app_conn, embedder, DOC.replace("Gamma", "Delta"), version=1)
    _, _, status = ingest_document(app_conn, embedder, DOC.replace("Gamma", "Delta"), version=2)
    assert status == "added"


def test_ingest_directory_loads_sample_docs(app_conn, embedder):
    lines = ingest_directory(app_conn, embedder, SAMPLE_DOCS)
    assert len(lines) == 3 and all(line.startswith("added") for line in lines)
    docs = app_conn.execute("SELECT title, version FROM documents ORDER BY title, version").fetchall()
    assert [(d["title"][:20], d["version"]) for d in docs] == [
        ("Mutual Non-Disclosur", 1),
        ("Shareholders' Agreem", 1),
        ("Shareholders' Agreem", 2),
    ]
    # Second run: nothing changes.
    assert all(line.startswith("unchanged") for line in ingest_directory(app_conn, embedder, SAMPLE_DOCS))


def test_ingest_directory_rejects_unversioned_file_names(app_conn, embedder, tmp_path: Path):
    (tmp_path / "contract.txt").write_text(DOC)
    with pytest.raises(IngestError, match="contract.v1.txt"):
        ingest_directory(app_conn, embedder, tmp_path)


# --- retrieval -----------------------------------------------------------------


def test_retrieval_uses_only_the_latest_version(app_conn, embedder, sample_corpus):
    results = retrieve(app_conn, embedder, "notice period for a transfer of shares", top_k=10)
    shareholder = [r for r in results if r.title.startswith("Shareholders")]
    assert shareholder, "expected shareholder agreement chunks"
    assert {r.document_version for r in shareholder} == {2}

    # v1 is still stored, so old answers that cited it remain traceable.
    v1_chunks = app_conn.execute(
        "SELECT count(*) AS n FROM chunks WHERE document_version = 1 AND document_id IN "
        "(SELECT id FROM documents WHERE title LIKE 'Shareholders%%')"
    ).fetchone()["n"]
    assert v1_chunks > 0


def test_retrieval_ranks_the_relevant_page_first(app_conn, embedder, sample_corpus):
    [top] = retrieve(app_conn, embedder, "How many days notice for a share transfer "
                                         "under the right of first refusal?", top_k=1)
    assert top.title.startswith("Shareholders") and top.page == 2
    assert "45 days" in top.content


def test_retrieval_returns_top_k_sorted_by_score(app_conn, embedder, sample_corpus):
    results = retrieve(app_conn, embedder, "confidential information", top_k=3)
    assert len(results) == 3
    assert [r.score for r in results] == sorted((r.score for r in results), reverse=True)


def test_audit_record_has_identity_and_hash_but_no_text(app_conn, embedder, sample_corpus):
    [top] = retrieve(app_conn, embedder, "governing law", top_k=1)
    record = top.audit_record()
    assert set(record) == {"chunk_id", "document_id", "document_version", "page",
                           "content_hash", "score"}
    assert record["content_hash"] == sha256_text(top.content)
