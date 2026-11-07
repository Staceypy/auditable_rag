# Auditable RAG Demo — Spec

## Goal
A small, runnable demo showing how to make a RAG system auditable for legal / governance use.
Every AI answer can later be reconstructed: what was asked, which document chunks were used,
which model and prompt version produced it, what the user saw, and who reviewed it.
Audit records are append-only and tamper-evident (hash chain), and a verifier script detects tampering.

This is a portfolio demo, not production. Keep it small, readable, and well tested.

## Tech stack
- Python 3.11+, managed with `uv` (or pip + venv if uv is unavailable)
- Postgres 16 with the `pgvector` extension, run via `docker compose`
- FastAPI for a minimal HTTP API
- OpenAI API for embeddings and chat (key read from `OPENAI_API_KEY` in `.env`)
- pytest for tests

## Project structure (suggested)
```
auditable-rag/
  docker-compose.yml
  .env.example
  README.md
  sql/
    001_schema.sql
  app/
    config.py        # model name pinned, prompt registry
    ingest.py        # load sample docs, chunk, embed, store with doc_version + chunk_hash
    retrieve.py      # vector search over chunks
    answer.py        # RAG answer with citations (doc, page, chunk)
    audit.py         # append audit events, compute hash chain
    api.py           # FastAPI endpoints
  scripts/
    verify_chain.py  # recompute hash chain, report first broken event
    tamper_demo.py   # deliberately edit one row (as superuser) to show detection
  data/
    sample_docs/     # 2–3 short fictional legal documents (e.g. a shareholder agreement)
  tests/
```

## Data model
`documents(id, title, version, created_at)`
`chunks(id, document_id, document_version, page, content, content_hash, embedding vector)`

`audit_events` (append-only):
- `seq` BIGSERIAL, `event_id` UUID, `event_type` TEXT
  (`ai.answer_generated`, `human.review`)
- `trace_id`, `tenant_id`, `matter_id`, `actor_id`, `actor_role`, `created_at`
- `payload` JSONB — for answers: query hash, retrieved chunks (chunk_id, document_version,
  page, content_hash), model name + params, prompt id + version, output hash;
  for reviews: ref_event_id, action (`accepted` / `edited` / `rejected`), edited output hash
- `prev_hash`, `hash` where `hash = sha256(prev_hash || canonical_json(event without hash))`
- Store full query/output text in a separate `audit_blobs(hash, content)` table and reference
  it by hash from the event (so content can later be removed for GDPR while the chain stays intact).

## Immutability rules
- Create an `app_user` role that can only INSERT and SELECT on `audit_events`.
  REVOKE UPDATE, DELETE, TRUNCATE.
- Add a trigger that raises an exception on any UPDATE or DELETE of `audit_events`.
- The app connects as `app_user`; only the tamper demo uses the superuser.
- Hash chain must use canonical JSON (sorted keys, no whitespace) so hashing is deterministic.
- Appending must be safe under concurrency (e.g. lock / serialize when reading the last hash).

## API
- `POST /ask` — body: `{question, tenant_id, matter_id, user_id, role}`.
  Returns answer, citations, and `event_id`. Writes an `ai.answer_generated` event.
- `POST /review` — body: `{event_id, action, edited_answer?, user_id, role}`.
  Writes a `human.review` event (never updates the original).
- `GET /audit/{matter_id}` — returns the event timeline for one matter, each AI answer
  joined with its review(s). Reading audit records should itself be logged.

## Scripts
- `verify_chain.py` — walks events in `seq` order, recomputes every hash, prints OK or the
  first broken `seq`. Exit code 1 on failure.
- `tamper_demo.py` — modifies one payload as superuser (bypassing the trigger), then runs the
  verifier to show the break is detected.

## Tests
- Hash chain: valid chain passes; modified, deleted, or reordered events fail.
- `app_user` cannot UPDATE or DELETE audit rows.
- `/ask` writes exactly one event with pinned model and prompt version.
- `/review` appends a new event and leaves the original unchanged.
- LLM and embedding calls are mocked in tests (no API key needed to run tests).

## README should explain
- How to run it in ~5 commands.
- Why observability (e.g. LangSmith) and audit logging are kept separate.
- Design decisions: snapshot by hash not just doc id, pinned model versions,
  reviews as new events, blobs separated for GDPR, auditable ≠ reproducible.
- What would change for production: export to S3 Object Lock, scheduled verifier with alerting,
  per-tenant encryption keys (crypto-shredding), access control per matter.

## Working style for Claude Code
- Start in plan mode: propose the plan and file list before writing code.
- Build in small steps; run tests after each step; commit after each working step.
- Keep code simple and commented where the audit logic is non-obvious.
