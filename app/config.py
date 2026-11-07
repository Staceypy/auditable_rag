"""Settings read from the environment (and .env).

Pinned model names and the prompt registry are added in step 5.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# The app and ingest connect as app_user, which can only SELECT and INSERT.
APP_DATABASE_URL = os.environ.get(
    "APP_DATABASE_URL",
    "postgresql://app_user:app_user_demo_password@localhost:5432/auditable_rag",
)

# Used only for schema setup, tests and scripts/tamper_demo.py.
SUPERUSER_DATABASE_URL = os.environ.get(
    "SUPERUSER_DATABASE_URL",
    "postgresql://postgres:postgres_demo_password@localhost:5432/auditable_rag",
)
