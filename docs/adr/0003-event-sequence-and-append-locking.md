# ADR 0003: `seq` as BIGSERIAL, assigned under an advisory lock

## Status
Accepted (2026-11-05, recorded 2026-11-11)

## Context
The hash chain ([ADR 0001](0001-append-only-hash-chained-audit-log.md)) needs one total order of events, and every new event must link to the true last event. Two problems follow:

- **Forks.** If two requests read the same "last hash" at the same time, both link to it and the chain splits. A one-off experiment without locking produced 4–7 distinct `prev_hash` values for 20 parallel appends, which means the chain had forked. The verifier caught it.
- **Order.** `seq` is part of the hash ([ADR 0002](0002-hash-input-format.md)), so it must be known *before* hashing, and seq order must match chain order.

The app role is deliberately weak: `app_user` has only `SELECT` and `INSERT`.

## Decision
- `seq` is `BIGSERIAL` (64-bit).
- `app/audit.py: append_event` does this in one transaction:
  1. take `pg_advisory_xact_lock` on a fixed key, held until commit;
  2. read the last hash;
  3. call `nextval()` for `seq` and assign `created_at` in the app;
  4. hash and insert;
  5. commit, which releases the lock.
- Gaps in `seq` are allowed. A rolled-back transaction still uses up a sequence value. The verifier checks the `prev_hash` links, not that `seq` numbers are consecutive.
- `append_event` refuses to run inside an already open transaction. There, its block would only be a savepoint: nothing would commit, and the lock would be held until the caller's transaction ended. Finding this bug led to the rule.

## Alternatives considered
- **`SERIAL` (32-bit).** It runs out after about 2.1 billion values, gaps included. `seq` is the primary key and part of every hash, so switching to 64-bit later would rewrite the whole append-only table under an exclusive lock. The hashes themselves wouldn't change, because the numbers stay the same. 64-bit costs 4 bytes per row. Rejected.
- **`LOCK TABLE audit_events IN EXCLUSIVE MODE`.** Postgres requires `UPDATE`, `DELETE` or `TRUNCATE` privilege for this mode, which `app_user` deliberately lacks. Tested: `SHARE ROW EXCLUSIVE` and `EXCLUSIVE` are refused, and `ROW EXCLUSIVE`, which is allowed, doesn't block other writers. Rejected.
- **A "chain head" row locked with `SELECT … FOR UPDATE`.** That needs a mutable row and `UPDATE` privilege, which goes against having an append-only log. Rejected.
- **`SERIALIZABLE` isolation with retries.** Correct, but every caller needs retry logic, and conflicts cause aborts under load. Rejected.
- **Order by `created_at`, or by UUIDv7 ids.** Clocks drift and values can tie, so neither gives a reliable total order. Rejected.

## Consequences
- All appends across all tenants are serialized. That's fine for an audit log written once per request, but it's a throughput limit at high volume, where you'd use one chain per tenant.
- The lock is held only for a few short statements. The model and embedding calls happen before the lock is taken.
- Appends assume the default `READ COMMITTED` isolation, so step 2 sees the event the previous lock holder committed. Callers must not wrap appends in `REPEATABLE READ` transactions.
