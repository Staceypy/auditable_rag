"""Database connection helper."""
import psycopg
from psycopg.rows import dict_row

from app import config


def connect(url: str | None = None) -> psycopg.Connection:
    """Open a connection that returns rows as dicts.

    Defaults to the restricted app_user connection.
    """
    return psycopg.connect(url or config.APP_DATABASE_URL, row_factory=dict_row)
