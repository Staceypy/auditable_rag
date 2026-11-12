# ADR 0006: Immutable document versions; answers pin chunks by content hash

## Status
Accepted (2026-11-05, recorded 2026-11-11)

## Context
An answer event must say which text the answer was based on. A document id alone doesn't: contracts get amended, and "document 3" may say something different next month. If old text is overwritten, past answers can no longer be explained, and that can't be repaired afterwards.

The sample data shows the problem. The shareholder agreement v2 changed the notice period from 30 to 45 days.

## Decision
- `documents` has one row per `(title, version)`, enforced unique. The version comes from the file name (`*.v2.txt`).
- Re-ingesting an unchanged version does nothing. Ingesting different text under an existing version is refused, so the text needs a new version number.
- Every chunk stores `document_version` and `content_hash` (sha256 of its text). Chunks never span pages, so every citation can name a page.
- Old versions and their chunks are never deleted. Retrieval searches only the newest version of each title.
- The answer event records, for each retrieved chunk, its chunk id, document id, version, page, `content_hash`, score and whether it was cited. It records no document text. The exact messages sent to the model can be rebuilt from this, and a test does so.

## Alternatives considered
- **Overwrite chunks on re-ingest.** Simple, but it destroys the evidence behind past answers. Rejected.
- **Copy chunk text into each answer event.** Self-contained, but documents get duplicated into every event, and `audit_blobs` already holds personal data, which document text shouldn't be mixed with. Rejected.
- **Version from a content hash instead of a number.** Automatic, but people refer to "version 2" and to amendment dates, not hashes. Rejected.

## Consequences
- Storage grows with every version, and old embeddings are kept even though they're no longer searched.
- Retrieval needs the "newest version per title" filter (`DISTINCT ON (title)`). A title change creates a new document, not a new version.
- Documents aren't scoped to a tenant or matter yet. Adding that later means a new column and a backfill, and the README lists it as a production change.
