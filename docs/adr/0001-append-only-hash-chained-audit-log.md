# ADR 0001: Append-only, hash-chained audit log in Postgres

## Status
Accepted (2026-11-05, recorded 2026-11-11)

## Context
For legal and governance use, every AI answer must be reconstructable later, and it must be possible to show that the record wasn't changed afterwards. "We have a log table" isn't enough evidence: anyone with database access could edit or delete rows, and nothing would show it.

The demo must stay small and run with `docker compose` and Postgres only (SPEC.md).

## Decision
Store audit events in one Postgres table, `audit_events`, protected by three independent layers:

1. **Privileges.** The app connects as `app_user`, which has only `SELECT` and `INSERT` on the audit tables. `UPDATE`, `DELETE` and `TRUNCATE` are revoked.
2. **Triggers.** A row trigger rejects `UPDATE` and `DELETE`, and a statement trigger rejects `TRUNCATE`, for every role including the superuser. Bypassing them needs a deliberate, superuser-only step (`SET session_replication_role = replica`).
3. **Hash chain.** Each event stores the previous event's hash, and its own hash covers that value plus all of its other columns (format in [ADR 0002](0002-hash-input-format.md)). `scripts/verify_chain.py` recomputes the chain and reports the first broken `seq`.

The first two layers *prevent* changes; the third *detects* changes that got past them.

## Alternatives considered
- **Plain table, privileges only.** Simple, but a superuser or anyone holding the database credentials can change history without a trace. Rejected: prevention without detection.
- **Dedicated ledger database** (e.g. immudb). Tamper evidence is built in, but it's a second datastore to run, back up and join against. Too heavy for a small demo. Rejected for now.
- **Per-event signatures instead of a chain.** Each event signed with a private key. Detects modified events, but not deleted or reordered ones, and adds key management. Rejected.
- **Merkle tree** (as in certificate transparency logs). Allows efficient proofs for single events, which a linear chain doesn't, but it's much more complex. Not needed at demo scale. Rejected.

## Consequences
- Verification is a linear scan of the whole table, which is fine for a demo and slow for very large logs. Periodic checkpoints would fix that.
- **Two gaps remain**, documented in the README: deleting the newest events leaves a shorter chain that still verifies, and a superuser can rewrite the whole chain and recompute every hash. Both are only closed by storing the chain head outside the database, via `--expect-head` and, in production, S3 Object Lock.
- Fixing a wrong event means appending a correcting event, never editing the original. [ADR 0005](0005-reviews-and-reads-as-new-events.md) applies this to reviews.
- Test setup and `tamper_demo.py` need the superuser, because no other role can empty or change the table.
