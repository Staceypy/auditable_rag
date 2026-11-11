# ADR 0005: Human reviews and audit reads are new events on the same chain

## Status
Accepted (2026-11-05, recorded 2026-11-11)

## Context
After an AI answer, a lawyer accepts, edits or rejects it. The record must show both what the AI said and what the human did with it. Separately, SPEC.md requires that reading the audit records is itself logged. It doesn't say where those read logs go, or how a reader identifies themselves; that was decided with the project owner during planning.

The event model is hard to change afterwards. Events can't be rewritten, so old and new shapes would have to coexist forever.

## Decision
- **Reviews:** `POST /review` appends a `human.review` event and never updates the answer event. Its payload holds:
  - `ref_event_id`;
  - `ref_event_hash`, which pins the exact version of the record that was reviewed;
  - `action`;
  - `edited_output_hash` for edits.

  The review takes its `tenant_id`, `matter_id` and `trace_id` from the answer, so it can't be filed under another matter. An answer can have several reviews.
- **Reads:** `GET /audit/{matter_id}` requires `tenant_id`, `user_id` and `role`, and appends an `audit.read` event recording what it returned (`returned_events`, `up_to_seq`). It goes on the same hash chain as all other events.
- `event_type` is limited to these three values by a `CHECK` constraint.

## Alternatives considered
- **Review columns on the answer row.** Needs `UPDATE`, which ADR 0001 forbids, and keeps only the latest review. Rejected.
- **Separate, unchained reviews table.** Reviews could then be changed or deleted without detection, even though they're the human half of the record. Rejected.
- **Separate, unchained access-log table for reads.** Simpler, and it would keep the chain to the two event types in the spec, but read records could then be deleted without a trace. Considered and rejected by the project owner during planning.

## Consequences
- **Reads grow the chain.** Every audit read adds an event, so the chain grows with reads as well as writes, and reads also wait for the append lock.
- **The current state is derived, not stored.** "Was this answer accepted?" means finding the latest review, and the timeline endpoint builds that view.
- Because reads are logged, an audit read can fail (with 503) when the log is unavailable, just as `/ask` does.
- The verifier itself doesn't log a read. It's a read-only integrity check, so it never changes the head it checks.
