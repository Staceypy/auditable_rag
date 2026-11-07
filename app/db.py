"""Database connection helper."""
import psycopg
from psycopg.rows import dict_row

from app import config


def connect(url: str | None = None) -> psycopg.Connection:
    """Open a connection that returns rows as dicts.

    Defaults to the restricted app_user connection. Autocommit is on, so a
    plain read never leaves a transaction open; multi-statement writes use an
    explicit `with conn.transaction():` block, which commits when it ends.
    """
    return psycopg.connect(url or config.APP_DATABASE_URL, row_factory=dict_row, autocommit=True)
