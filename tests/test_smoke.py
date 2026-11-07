"""Step 1 smoke test: Postgres is reachable and pgvector is installed.

Replaced by the real fixtures in step 2.
"""
import os

import psycopg
from dotenv import load_dotenv

load_dotenv()

SUPERUSER_URL = os.environ.get(
    "SUPERUSER_DATABASE_URL",
    "postgresql://postgres:postgres_demo_password@localhost:5432/auditable_rag",
)


def test_postgres_has_pgvector():
    with psycopg.connect(SUPERUSER_URL) as conn:
        version = conn.execute("SHOW server_version").fetchone()[0]
        available = conn.execute(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
        ).fetchone()
    assert version.startswith("16.")
    assert available is not None
