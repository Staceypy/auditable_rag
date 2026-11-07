"""Hash chain: a valid chain verifies, and each kind of tampering is caught."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import audit, db
from tests.conftest import TEST_APP_URL


def append(conn, n: int = 0, **payload_extra) -> dict:
    """Append a test event whose payload exercises JSONB normalization."""
    question = f"question {n}"
    payload = {
        # Deliberately unsorted keys, unicode, a float and nesting: JSONB
        # reorders and reformats these, and the hash must not change.
        "z_last": True,
        "a_first": {"nested": [3, 1, 2], "temperature": 0.2},
        "note": "Gesellschaftervertrag § 4 — ✓",
        "query_hash": audit.sha256_text(question),
        **payload_extra,
    }
    return audit.append_event(
        conn,
        event_type="ai.answer_generated",
        tenant_id="tenant-a",
        matter_id="matter-1",
        actor_id="user-1",
        actor_role="lawyer",
        payload=payload,
        blobs=[question],
    )


def tamper(super_conn, sql: str, params=()) -> None:
    """Change audit rows as the superuser, with the protective triggers off."""
    with super_conn.transaction():
        super_conn.execute("SET LOCAL session_replication_role = replica")
        super_conn.execute(sql, params)


@pytest.fixture
def chain(app_conn) -> list[dict]:
    """Five events appended through the real code path."""
    return [append(app_conn, n) for n in range(5)]


# --- canonical JSON ---------------------------------------------------------


def test_canonical_json_ignores_key_order_and_whitespace():
    assert audit.canonical_json({"b": 1, "a": [1, {"d": 2, "c": 3}]}) == \
        audit.canonical_json({"a": [1, {"c": 3, "d": 2}], "b": 1})
    assert audit.canonical_json({"a": 1, "b": "x"}) == b'{"a":1,"b":"x"}'


def test_canonical_json_rejects_nan():
    with pytest.raises(ValueError):
        audit.canonical_json({"x": float("nan")})


# --- valid chain --------------------------------------------------------------


def test_valid_chain_passes(app_conn, chain):
    result = audit.verify_database(app_conn)
    assert result.ok, result.reason
    assert result.checked == 5
    assert result.head_seq == chain[-1]["seq"]
    assert result.head_hash == chain[-1]["hash"]


def test_events_link_to_each_other(chain):
    assert chain[0]["prev_hash"] == audit.GENESIS_HASH
    for before, after in zip(chain, chain[1:]):
        assert after["prev_hash"] == before["hash"]


def test_empty_chain_passes():
    assert audit.verify_chain([]).ok


def test_expected_head_passes_on_intact_and_extended_chain(app_conn, chain):
    head = (chain[-1]["seq"], chain[-1]["hash"])
    assert audit.verify_database(app_conn, expect_head=head).ok
    append(app_conn, 99)  # Events appended after the head was saved are fine.
    assert audit.verify_database(app_conn, expect_head=head).ok


# --- tampering -------------------------------------------------------------


def test_modified_payload_fails(app_conn, super_conn, chain):
    tamper(super_conn, "UPDATE audit_events SET payload = payload || '{\"z_last\": false}' "
                       "WHERE seq = %s", (chain[2]["seq"],))
    result = audit.verify_database(app_conn)
    assert not result.ok
    assert result.broken_seq == chain[2]["seq"]
    assert "modified" in result.reason


def test_modified_column_fails(app_conn, super_conn, chain):
    tamper(super_conn, "UPDATE audit_events SET actor_id = 'someone-else' WHERE seq = %s",
           (chain[1]["seq"],))
    result = audit.verify_database(app_conn)
    assert not result.ok
    assert result.broken_seq == chain[1]["seq"]


@pytest.mark.parametrize("index", [0, 2])  # first event, middle event
def test_deleted_event_fails(app_conn, super_conn, chain, index):
    tamper(super_conn, "DELETE FROM audit_events WHERE seq = %s", (chain[index]["seq"],))
    result = audit.verify_database(app_conn)
    assert not result.ok
    # Reported at the event after the gap, whose prev_hash no longer matches.
    assert result.broken_seq == chain[index + 1]["seq"]
    assert "deleted or reordered" in result.reason


def test_deleted_tail_is_only_caught_with_expected_head(app_conn, super_conn, chain):
    head = (chain[-1]["seq"], chain[-1]["hash"])
    tamper(super_conn, "DELETE FROM audit_events WHERE seq >= %s", (chain[3]["seq"],))

    # Without a saved head, the shorter chain is still internally consistent.
    assert audit.verify_database(app_conn).ok

    result = audit.verify_database(app_conn, expect_head=head)
    assert not result.ok
    assert result.broken_seq == head[0]
    assert "deleted from the end" in result.reason


def test_wrong_expected_head_hash_fails(app_conn, chain):
    result = audit.verify_database(app_conn, expect_head=(chain[-1]["seq"], "f" * 64))
    assert not result.ok
    assert "differs from the expected head" in result.reason


def test_reordered_events_fail(app_conn, chain):
    events = audit.load_chain(app_conn)
    events[1], events[2] = events[2], events[1]
    result = audit.verify_chain(events)
    assert not result.ok
    assert result.broken_seq == chain[2]["seq"]


def test_renumbered_seq_fails(app_conn, super_conn, chain):
    # seq is part of each event's hash, so renumbering counts as a modification.
    tamper(super_conn, "UPDATE audit_events SET seq = seq + 100 WHERE seq = %s",
           (chain[-1]["seq"],))
    result = audit.verify_database(app_conn)
    assert not result.ok
    assert result.broken_seq == chain[-1]["seq"] + 100


# --- blobs -------------------------------------------------------------------


def test_modified_blob_fails(app_conn, super_conn, chain):
    super_conn.execute("UPDATE audit_blobs SET content = 'something else' WHERE hash = %s",
                       (chain[1]["payload"]["query_hash"],))
    super_conn.commit()
    result = audit.verify_database(app_conn)
    assert not result.ok
    assert result.broken_seq == chain[1]["seq"]
    assert "blob" in result.reason


def test_redacted_blob_keeps_chain_valid(app_conn, super_conn, chain):
    """GDPR erasure: deleting stored text leaves every event, and the chain, intact."""
    removed = chain[1]["payload"]["query_hash"]
    super_conn.execute("DELETE FROM audit_blobs WHERE hash = %s", (removed,))
    super_conn.commit()
    result = audit.verify_database(app_conn)
    assert result.ok, result.reason
    assert result.redacted_blobs == [removed]


def test_event_and_blob_are_written_atomically(app_conn):
    """If the event insert fails, its blob is rolled back too."""
    with pytest.raises(ValueError):
        audit.append_event(
            app_conn, event_type="ai.answer_generated", tenant_id="t", matter_id="m",
            actor_id="u", actor_role="r", payload={"bad": float("nan")},
            blobs=["orphan text"],
        )
    assert app_conn.execute("SELECT count(*) AS n FROM audit_blobs").fetchone()["n"] == 0
    assert app_conn.execute("SELECT count(*) AS n FROM audit_events").fetchone()["n"] == 0


# --- concurrency -------------------------------------------------------------


def test_concurrent_appends_keep_a_single_chain(app_conn):
    """Without the advisory lock, writers would read the same last hash and fork the chain."""
    def worker(n: int) -> None:
        with db.connect(TEST_APP_URL) as conn:
            append(conn, n)

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(worker, range(20)))

    result = audit.verify_database(app_conn)
    assert result.ok, result.reason
    assert result.checked == 20
