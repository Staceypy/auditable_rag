# Architecture Decision Records

Short records of decisions that are hard to undo, meaning that changing them later would need a data migration or a large refactor. Each one follows Michael Nygard's format, plus a section on the alternatives considered.

| ADR | Decision |
|---|---|
| [0001](0001-append-only-hash-chained-audit-log.md) | Append-only, hash-chained audit log in Postgres |
| [0002](0002-hash-input-format.md) | Hash input format for audit events |
| [0003](0003-event-sequence-and-append-locking.md) | `seq` as BIGSERIAL, assigned under an advisory lock |
| [0004](0004-texts-in-content-addressed-blob-table.md) | Question and answer text in a separate, content-addressed blob table |
| [0005](0005-reviews-and-reads-as-new-events.md) | Human reviews and audit reads are new events on the same chain |
| [0006](0006-immutable-document-versions-pinned-by-hash.md) | Immutable document versions; answers pin chunks by content hash |
| [0007](0007-pinned-models-prompt-registry-and-embedding-dimension.md) | Pinned model snapshot, versioned prompt registry, fixed embedding dimension |

ADRs 0001–0007 were decided during planning on 2026-11-05, built on 2026-11-07, and written down on 2026-11-11.

## Rules

- **One file per decision**, named `NNNN-short-title.md`.
- **Never rewrite an accepted ADR.** To change a decision, add a new ADR that says "Supersedes ADR NNNN", and set the old one's status to `Superseded by ADR MMMM`.
- **Only write one for decisions that are hard to undo.** Renaming a function doesn't need an ADR; changing the hash format does.
