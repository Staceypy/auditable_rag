"""Append-only, hash-chained audit log.

Every event stores the hash of the event before it, and its own hash covers
that value plus all of its other columns:

    hash = sha256(prev_hash || canonical_json(every column except hash))

Changing, deleting or reordering any event therefore breaks the link to the
event that follows it, and the verifier below reports the first event where
the chain no longer holds.

Full question and answer text is not stored in events. It goes in
audit_blobs, keyed by its own sha256, and events only hold that hash. A blob
can later be deleted (e.g. for GDPR) without touching any event, so the chain
still verifies; the verifier reports such blobs as redacted.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Iterable

import psycopg
from psycopg.types.json import Jsonb

GENESIS_HASH = "0" * 64

# Payload fields that hold the hash of an audit_blobs row.
BLOB_FIELDS = ("query_hash", "output_hash", "edited_output_hash")

# Columns covered by the hash, i.e. everything except `hash` itself.
HASHED_COLUMNS = (
    "seq",
    "event_id",
    "event_type",
    "trace_id",
    "tenant_id",
    "matter_id",
    "actor_id",
    "actor_role",
    "created_at",
    "payload",
    "prev_hash",
)

# Arbitrary constant key for the advisory lock that serializes appends.
_APPEND_LOCK_KEY = 7_202_611


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def canonical_json(value) -> bytes:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8.

    Postgres JSONB doesn't keep key order or whitespace, so the hash must not
    depend on either. NaN/Infinity are rejected because they aren't valid JSON
    and JSONB can't store them.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def format_timestamp(ts: datetime) -> str:
    """One fixed text form for created_at: UTC, microseconds, 'Z' suffix.

    Postgres stores microseconds, so a value read back from the database
    formats exactly the same as the value that was hashed before inserting.
    """
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def compute_hash(event: dict) -> str:
    """Hash an event, given as a dict of at least HASHED_COLUMNS.

    Values are normalized first, so a row read back from the database (UUID
    and datetime objects) hashes the same as the values used when inserting.
    """
    fields = {col: event[col] for col in HASHED_COLUMNS}
    fields["seq"] = int(fields["seq"])
    fields["event_id"] = str(fields["event_id"])
    fields["created_at"] = format_timestamp(fields["created_at"])
    data = event["prev_hash"].encode("ascii") + canonical_json(fields)
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def put_blob(conn: psycopg.Connection, content: str) -> str:
    """Store text in audit_blobs and return its hash.

    Identical text is stored once (content addressing), so re-inserting is a
    no-op.
    """
    blob_hash = sha256_text(content)
    conn.execute(
        "INSERT INTO audit_blobs (hash, content) VALUES (%s, %s) ON CONFLICT (hash) DO NOTHING",
        (blob_hash, content),
    )
    return blob_hash


def append_event(
    conn: psycopg.Connection,
    *,
    event_type: str,
    tenant_id: str,
    matter_id: str,
    actor_id: str,
    actor_role: str,
    payload: dict,
    trace_id: str | None = None,
    blobs: Iterable[str] = (),
) -> dict:
    """Append one event to the chain and return it, hash included.

    `blobs` are texts to store in audit_blobs in the same transaction; the
    caller puts their hashes in the payload (see put_blob / sha256_text).

    Appends must be serialized: if two writers read the same last hash, both
    would link to it and the chain would fork. So, in one transaction:
      1. take a transaction-level advisory lock (held until commit),
      2. read the current last hash,
      3. assign seq and created_at, hash, insert, commit.
    seq is assigned inside the lock, so seq order is the same as chain order.

    Assumes the default READ COMMITTED isolation, where step 2 sees the event
    committed by the previous lock holder.
    """
    with conn.transaction():
        for text in blobs:
            put_blob(conn, text)

        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_APPEND_LOCK_KEY,))
        last = conn.execute(
            "SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        seq = conn.execute("SELECT nextval('audit_events_seq_seq') AS seq").fetchone()["seq"]

        event = {
            "seq": seq,
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "trace_id": trace_id or str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "matter_id": matter_id,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "created_at": datetime.now(UTC),
            "payload": payload,
            "prev_hash": last["hash"] if last else GENESIS_HASH,
        }
        event["hash"] = compute_hash(event)

        conn.execute(
            """
            INSERT INTO audit_events (seq, event_id, event_type, trace_id, tenant_id,
                                      matter_id, actor_id, actor_role, created_at,
                                      payload, prev_hash, hash)
            VALUES (%(seq)s, %(event_id)s, %(event_type)s, %(trace_id)s, %(tenant_id)s,
                    %(matter_id)s, %(actor_id)s, %(actor_role)s, %(created_at)s,
                    %(payload)s, %(prev_hash)s, %(hash)s)
            """,
            {**event, "payload": Jsonb(payload)},
        )
    return event


# ---------------------------------------------------------------------------
# Verifying
# ---------------------------------------------------------------------------


@dataclass
class VerifyResult:
    ok: bool
    checked: int = 0
    head_seq: int | None = None
    head_hash: str | None = None
    broken_seq: int | None = None
    reason: str | None = None
    redacted_blobs: list[str] = field(default_factory=list)


def verify_chain(
    events: list[dict],
    blobs: dict[str, str] | None = None,
    expect_head: tuple[int, str] | None = None,
) -> VerifyResult:
    """Walk events in the given order and stop at the first broken one.

    Pure function: `events` are rows as read from audit_events (normally
    ordered by seq), `blobs` maps blob hash to content.

    What each check catches:
      - prev_hash link broken: an event was deleted, or events reordered.
      - hash mismatch: an event's columns were modified.
      - blob content doesn't match its hash: a stored text was modified.
        A blob that's simply missing was redacted, which isn't a failure.
      - expect_head: a head (seq, hash) saved earlier, ideally outside the
        database. Deleting the newest events leaves a valid but shorter
        chain, which only a saved head can reveal.

    It cannot catch someone with database access rewriting the whole chain
    and recomputing every hash; that needs the head stored externally too.
    """
    result = VerifyResult(ok=True)
    prev_hash = GENESIS_HASH
    seen_head = False

    def fail(seq: int | None, reason: str) -> VerifyResult:
        result.ok = False
        result.broken_seq = seq
        result.reason = reason
        return result

    for event in events:
        seq = event["seq"]
        if event["prev_hash"] != prev_hash:
            return fail(seq, "prev_hash does not match the previous event "
                             "(an event was deleted or reordered)")
        if compute_hash(event) != event["hash"]:
            return fail(seq, "hash mismatch (event was modified)")

        if blobs is not None:
            for name in BLOB_FIELDS:
                blob_hash = event["payload"].get(name)
                if blob_hash is None:
                    continue
                if blob_hash not in blobs:
                    result.redacted_blobs.append(blob_hash)
                elif sha256_text(blobs[blob_hash]) != blob_hash:
                    return fail(seq, f"blob {name} does not match its hash (blob was modified)")

        if expect_head and seq == expect_head[0]:
            if event["hash"] != expect_head[1]:
                return fail(seq, "hash differs from the expected head")
            seen_head = True

        prev_hash = event["hash"]
        result.checked += 1
        result.head_seq = seq
        result.head_hash = event["hash"]

    if expect_head and not seen_head:
        return fail(expect_head[0], "expected head event is missing "
                                    "(events were deleted from the end of the chain)")
    return result


def load_chain(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM audit_events ORDER BY seq").fetchall()


def load_blobs(conn: psycopg.Connection) -> dict[str, str]:
    return {r["hash"]: r["content"] for r in conn.execute("SELECT hash, content FROM audit_blobs")}


def verify_database(
    conn: psycopg.Connection, expect_head: tuple[int, str] | None = None
) -> VerifyResult:
    """Verify the chain and blobs stored in the database `conn` points at."""
    return verify_chain(load_chain(conn), load_blobs(conn), expect_head)
