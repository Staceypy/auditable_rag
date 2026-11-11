# ADR 0004: Store question and answer text in a separate, content-addressed blob table

## Status
Accepted (2026-11-05, recorded 2026-11-11)

## Context
Questions, answers and edited answers can contain personal data, such as client names or salary details. Under GDPR, a person can ask for their data to be erased. But audit events can never be changed or deleted ([ADR 0001](0001-append-only-hash-chained-audit-log.md)), and deleting one would break the chain anyway.

Moving existing text out of events later would mean rewriting every event, which this design forbids. So the decision had to be made before the first event was written.

## Decision
- Full texts go in `audit_blobs(hash, content)`, keyed by the sha256 of the text.
- Events only store the hashes, as `query_hash`, `output_hash` and `edited_output_hash`.
- The event and its blobs are written in the same transaction ([ADR 0003](0003-event-sequence-and-append-locking.md)).
- `app_user` can insert blobs but not delete them. Erasure is a privileged, out-of-band step.
- The verifier rehashes every blob. A changed blob counts as tampering; a missing one is reported as redacted, and the chain still verifies. The API returns redacted text as `null`.

## Alternatives considered
- **Text inside the event payload.** Simplest, but erasure is impossible without breaking the chain. Rejected.
- **No text at all, only hashes.** Then the record can't show what was asked or answered. Rejected.
- **Encrypted text in the payload, with per-tenant or per-person keys (crypto-shredding).** Erasure means destroying the key, which also covers backups. This is the production direction, but it needs key management. Rejected for the demo; listed in the README.

## Consequences
- **Hashes outlive erasure.** For short or guessable texts (like "yes"), someone could find the text by hashing candidates. A keyed hash (HMAC) per tenant would prevent this.
- **Identical texts share one blob, across tenants.** Because the key is only the hash, two tenants asking the exact same question share one row. Erasing it for one tenant also removes it for the other. Their events still verify, but the other tenant's text is gone. A keyed hash per tenant would fix this too, since the same text would then get a different key in each tenant. That changes the meaning of every stored hash, so it should be decided before real data exists.
- Erasure has no tool yet: it's a manual `DELETE` as the superuser.
