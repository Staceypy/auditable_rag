"""Load documents, split them into page-bound chunks, embed and store them.

    uv run python -m app.ingest [DIRECTORY]      # default: data/sample_docs

Document files are plain text:
  - the first line is the title, as "# Title",
  - pages start with a "--- page N ---" marker,
  - the version comes from the file name: name.v2.txt is version 2.

Versions are never overwritten. Re-ingesting an unchanged version is a no-op;
changed text needs a new version number. Old versions and their chunks stay
in the database, so past answers can still be traced to the exact text
they were based on (see the content_hash of each chunk).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg

from app import db
from app.audit import sha256_text
from app.embeddings import Embedder, OpenAIEmbedder, to_vector_literal

SAMPLE_DOCS = Path(__file__).resolve().parent.parent / "data" / "sample_docs"
MAX_CHUNK_CHARS = 800

_PAGE_MARKER = re.compile(r"^--- page (\d+) ---$", re.M)
_VERSIONED_NAME = re.compile(r"^(?P<name>.+)\.v(?P<version>\d+)\.txt$")


class IngestError(ValueError):
    pass


@dataclass(frozen=True)
class Chunk:
    page: int
    content: str

    @property
    def content_hash(self) -> str:
        return sha256_text(self.content)


# ---------------------------------------------------------------------------
# Parsing and chunking (pure)
# ---------------------------------------------------------------------------


def parse_document(text: str) -> tuple[str, list[tuple[int, str]]]:
    """Split a document file into its title and (page number, page text) pairs."""
    first_line, _, body = text.partition("\n")
    if not first_line.startswith("# "):
        raise IngestError('the first line must be the title, as "# Title"')
    title = first_line[2:].strip()

    parts = _PAGE_MARKER.split(body)
    # parts = [text before first marker, page no, page text, page no, page text, ...]
    if parts[0].strip():
        raise IngestError("text found before the first '--- page N ---' marker")
    pages = [(int(number), page_text.strip()) for number, page_text in zip(parts[1::2], parts[2::2])]
    if not pages:
        raise IngestError("no '--- page N ---' markers found")
    return title, pages


def chunk_pages(pages: list[tuple[int, str]], max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
    """Pack paragraphs into chunks of at most max_chars, never crossing a page.

    Chunks stay within one page so every citation can name its page. A single
    paragraph longer than max_chars becomes its own chunk rather than being
    cut mid-sentence.
    """
    chunks: list[Chunk] = []
    for page, text in pages:
        current: list[str] = []
        for paragraph in (p.strip() for p in text.split("\n\n")):
            if not paragraph:
                continue
            if current and len("\n\n".join([*current, paragraph])) > max_chars:
                chunks.append(Chunk(page, "\n\n".join(current)))
                current = []
            current.append(paragraph)
        if current:
            chunks.append(Chunk(page, "\n\n".join(current)))
    return chunks


def version_from_filename(path: Path) -> int:
    match = _VERSIONED_NAME.match(path.name)
    if not match:
        raise IngestError(f"{path.name}: expected a name like contract.v1.txt")
    return int(match["version"])


# ---------------------------------------------------------------------------
# Storing
# ---------------------------------------------------------------------------


def ingest_document(
    conn: psycopg.Connection, embedder: Embedder, text: str, version: int
) -> tuple[str, int, str]:
    """Store one document version. Returns (title, document id, "added" | "unchanged")."""
    title, pages = parse_document(text)
    chunks = chunk_pages(pages)

    existing = conn.execute(
        "SELECT id FROM documents WHERE title = %s AND version = %s", (title, version)
    ).fetchone()
    if existing:
        stored = [r["content_hash"] for r in conn.execute(
            "SELECT content_hash FROM chunks WHERE document_id = %s ORDER BY id", (existing["id"],)
        )]
        if stored != [c.content_hash for c in chunks]:
            raise IngestError(
                f"{title!r} version {version} is already stored with different text; "
                "use a new version number instead of changing it"
            )
        return title, existing["id"], "unchanged"

    # Embed before opening the transaction, so no lock waits on the API call.
    # The title is prepended so chunks like "Clause 4 ..." still match
    # questions that name the document.
    vectors = embedder.embed([f"{title}\n\n{c.content}" for c in chunks])

    with conn.transaction():
        document_id = conn.execute(
            "INSERT INTO documents (title, version) VALUES (%s, %s) RETURNING id", (title, version)
        ).fetchone()["id"]
        for chunk, vector in zip(chunks, vectors, strict=True):
            conn.execute(
                """
                INSERT INTO chunks (document_id, document_version, page, content,
                                    content_hash, embedding)
                VALUES (%s, %s, %s, %s, %s, %s::vector)
                """,
                (document_id, version, chunk.page, chunk.content, chunk.content_hash,
                 to_vector_literal(vector)),
            )
    return title, document_id, "added"


def ingest_directory(conn: psycopg.Connection, embedder: Embedder, directory: Path) -> list[str]:
    """Ingest every *.vN.txt file, lowest version first. Returns one log line per file."""
    def order(path: Path) -> tuple[str, int]:
        version = version_from_filename(path)  # also rejects badly named files
        return _VERSIONED_NAME.match(path.name)["name"], version

    files = sorted(directory.glob("*.txt"), key=order)
    if not files:
        raise IngestError(f"no .txt files in {directory}")
    lines = []
    for path in files:
        version = version_from_filename(path)
        title, document_id, status = ingest_document(conn, embedder, path.read_text("utf-8"), version)
        lines.append(f"{status:9} {title} v{version} (document {document_id})")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    directory = Path(args[0]) if args else SAMPLE_DOCS
    with db.connect() as conn:
        for line in ingest_directory(conn, OpenAIEmbedder(), directory):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
