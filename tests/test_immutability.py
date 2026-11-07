"""The database itself enforces that audit_events is append-only.

Two layers: app_user lacks the privileges (grants), and even the superuser is
stopped by triggers unless it deliberately switches them off.
"""
import uuid

import psycopg
import pytest
from psycopg import errors


def insert_raw_event(conn) -> int:
    """Insert one syntactically valid event; the hash values don't matter here."""
    row = conn.execute(
        """
        INSERT INTO audit_events (event_id, event_type, trace_id, tenant_id, matter_id,
                                  actor_id, actor_role, created_at, payload, prev_hash, hash)
        VALUES (%s, 'audit.read', 't', 'tenant', 'matter', 'u1', 'lawyer', now(),
                '{}', %s, %s)
        RETURNING seq
        """,
        (uuid.uuid4(), "0" * 64, uuid.uuid4().hex * 2),
    ).fetchone()
    conn.commit()
    return row["seq"]


def test_app_user_can_insert_and_select(app_conn):
    seq = insert_raw_event(app_conn)
    row = app_conn.execute("SELECT seq FROM audit_events").fetchone()
    assert row["seq"] == seq


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET payload = '{\"tampered\": true}'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ],
)
def test_app_user_cannot_modify_events(app_conn, statement):
    insert_raw_event(app_conn)
    with pytest.raises(errors.InsufficientPrivilege):
        app_conn.execute(statement)


def test_app_user_cannot_bypass_triggers(app_conn):
    with pytest.raises(errors.InsufficientPrivilege):
        app_conn.execute("SET session_replication_role = replica")


def test_app_user_cannot_delete_blobs(app_conn):
    app_conn.execute("INSERT INTO audit_blobs (hash, content) VALUES (%s, 'x')", ("a" * 64,))
    app_conn.commit()
    with pytest.raises(errors.InsufficientPrivilege):
        app_conn.execute("DELETE FROM audit_blobs")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET payload = '{\"tampered\": true}'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ],
)
def test_triggers_stop_even_the_superuser(app_conn, super_conn, statement):
    insert_raw_event(app_conn)
    with pytest.raises(errors.RaiseException, match="append-only"):
        super_conn.execute(statement)


def test_database_has_pgvector(super_conn):
    version = super_conn.execute("SHOW server_version").fetchone()["server_version"]
    assert version.startswith("16.")
    assert super_conn.execute(
        "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
    ).fetchone()
