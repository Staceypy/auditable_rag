# ADR 0007: Pinned model snapshot, versioned prompt registry, fixed embedding dimension

## Status
Accepted (2026-11-05, recorded 2026-11-11)

## Context
"Which model and prompt produced this answer?" needs an exact answer that stays true later.

- **Model aliases move.** Aliases like `gpt-4o-mini` can start pointing at a different model without notice, so an old record that says "gpt-4o-mini" becomes ambiguous.
- **Prompts get edited.** A version label alone doesn't prove which text was used.
- **The embedding dimension is fixed in the schema.** The `vector(1536)` column has a fixed size, and chunk and question embeddings only match if one model produced both. Changing the embedding model means a schema change and re-embedding every chunk.

## Decision
- **Chat model:** pinned to a dated snapshot, `gpt-4o-mini-2024-07-18`, with fixed parameters (`temperature 0`, `max_completion_tokens 600`) in `app/config.py`. Each answer event records:
  - the model requested and the model OpenAI reports;
  - `system_fingerprint` and `finish_reason`;
  - the parameters.

  On 2026-11-07, a live run showed the reported model matching the snapshot.
- **Prompts:** kept in a registry keyed by `(id, version)`. Each has a hash of its full definition, which is recorded in the event. Prompts are never edited once used. A test pins the hash of `rag_answer v1`, so an in-place edit fails the test.
- **Embeddings:** `text-embedding-3-small` at 1536 dimensions, requested explicitly. The model name is recorded in each answer event.

## Alternatives considered
- **Model alias** (`gpt-4o-mini`). Picks up improvements automatically, but old records become ambiguous. Rejected.
- **Prompts as files without a hash, versioned only by git.** The event would then name a version without proof of the text. Rejected.
- **Store the full rendered prompt in every event.** Self-contained, but it duplicates document text into events (see [ADR 0006](0006-immutable-document-versions-pinned-by-hash.md)), and the messages can be rebuilt anyway. Rejected.
- **A larger embedding model** (`text-embedding-3-large`, up to 3072 dimensions). Better recall, but it costs more and the demo corpus is tiny. Rejected for now.

## Consequences
- **Snapshots retire.** OpenAI eventually withdraws dated snapshots, so the pin must be moved on purpose, and a new pin makes a visible change in new events. Old events stay explainable, but they can't be re-run on the same model. Auditable isn't the same as reproducible; see the README.
- **Embedding models have no dated snapshots.** `text-embedding-3-small` is the most precise pin available, so a silent provider-side change can't be ruled out.
- **Changing the embedding model is a migration:** new column or dimension, re-embed every chunk version, and keep the old model name for old events.
