"""Test database setup.

Tests run against a separate database, auditable_rag_test, in the same
Postgres container. It is dropped and rebuilt from sql/001_schema.sql once per
test session, and every table is emptied before each test.

Setup needs the superuser: audit_events can't be emptied any other way, which
is the point of the schema.
"""
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from app import config, db

TEST_DB = "auditable_rag_test"
SCHEMA = Path(__file__).resolve().parent.parent / "sql" / "001_schema.sql"

TEST_SUPERUSER_URL = make_conninfo(config.SUPERUSER_DATABASE_URL, dbname=TEST_DB)
TEST_APP_URL = make_conninfo(config.APP_DATABASE_URL, dbname=TEST_DB)


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    with psycopg.connect(config.SUPERUSER_DATABASE_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {TEST_DB}")
    with psycopg.connect(TEST_SUPERUSER_URL) as conn:
        conn.execute(SCHEMA.read_text())


@pytest.fixture(autouse=True)
def _clean_tables(_test_database):
    with psycopg.connect(TEST_SUPERUSER_URL) as conn:
        # Replica mode skips the append-only triggers so TRUNCATE can run.
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(
            "TRUNCATE audit_events, audit_blobs, chunks, documents RESTART IDENTITY"
        )


@pytest.fixture
def app_conn():
    """A connection as app_user, the role the application uses."""
    with db.connect(TEST_APP_URL) as conn:
        yield conn


@pytest.fixture
def super_conn():
    """A superuser connection, for simulating tampering."""
    with db.connect(TEST_SUPERUSER_URL) as conn:
        yield conn
