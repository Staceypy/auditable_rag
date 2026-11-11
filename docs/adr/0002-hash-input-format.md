# ADR 0002: Hash input format for audit events

## Status
Accepted (2026-11-05, recorded 2026-11-11)

## Context
[ADR 0001](0001-append-only-hash-chained-audit-log.md) chains events by hash. The exact bytes that get hashed are the hardest decision in the project to change. Every stored hash depends on them, so changing the format later makes every existing event fail verification. Old events can't be rehashed in place either, because the table is append-only.

The format must be deterministic. Postgres `JSONB` doesn't preserve key order or whitespace, and timestamps come back from the database as objects, not as the string that was hashed.

## Decision
```
hash = sha256(prev_hash || canonical_json(every column except hash))
```
- **Covered columns:** `seq`, `event_id`, `event_type`, `trace_id`, `tenant_id`, `matter_id`, `actor_id`, `actor_role`, `created_at`, `payload`, `prev_hash`. Leaving `seq` and `created_at` out would let them be changed without detection.
- **Canonical JSON:** Python `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`, encoded as UTF-8. NaN and Infinity are rejected because they aren't valid JSON and `JSONB` can't store them.
- **Normalized values:** `created_at` as UTC with microseconds and a `Z` suffix (`%Y-%m-%dT%H:%M:%S.%fZ`), `event_id` as a string, `seq` as an integer.
- **First event:** `prev_hash` is 64 zeros.

`app/audit.py` (`canonical_json`, `compute_hash`) is the only implementation. A test checks that payloads with unordered keys, Unicode, floats and nesting hash the same after a round trip through `JSONB`.

## Alternatives considered
- **RFC 8785 (JSON Canonicalization Scheme).** A standard, so verifiers in any language agree. But it needs an extra library, and the demo has a single verifier written in Python. Rejected for the demo; this is the main thing to revisit (see Consequences).
- **Hash only the payload.** Simpler, but actor, tenant, timestamp and order could then be changed undetected. Rejected.
- **Hash the row as Postgres prints it** (`row_to_json`). It ties the format to Postgres version details, and the hash couldn't be computed before inserting. Rejected.
- **Store the canonical bytes next to the hash.** Verification becomes trivial, but every event is stored twice, and the stored bytes could be edited along with the hash. Rejected.

## Consequences
- **No format version field.** Changing the format later means adding a `hash_version` column and supporting both formats in the verifier, or closing the old chain and starting a new one that links to the old head. Adding the column now would have been cheap; doing it later is the expensive version of this decision.
- **Verifiers in other languages must reproduce Python's `json.dumps` exactly.** It differs from RFC 8785 for some floats (Python writes `1.0`, RFC 8785 writes `1`) and for non-BMP characters in keys. Payloads avoid such values in practice, but nothing enforces that.
- Payload values must survive a round trip through `JSONB` unchanged. Strings, integers, booleans and ordinary floats are tested to do so.
