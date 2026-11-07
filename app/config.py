"""Settings, pinned model versions and the prompt registry.

Everything an audit record needs to say "which model and which prompt
produced this answer" is defined here, as fixed values, so each answer
event can record them exactly.
"""
import hashlib
import json
import os
from dataclasses import asdict, dataclass

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

# A dated snapshot, not the moving alias "gpt-4o-mini": the alias can start
# pointing at a different model without notice, which would make the model
# name in old audit records ambiguous.
CHAT_MODEL = "gpt-4o-mini-2024-07-18"
CHAT_PARAMS = {"temperature": 0, "max_completion_tokens": 600}

# OpenAI publishes no dated snapshots of embedding models, so this name is the
# most precise pin available. Chunk embeddings and query embeddings must come
# from the same model, or retrieval silently degrades.
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536  # must match vector(1536) in sql/001_schema.sql

TOP_K = 4  # chunks retrieved per question

# ---------------------------------------------------------------------------
# Prompt registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    system: str
    user_template: str  # formatted with {context} and {question}

    @property
    def hash(self) -> str:
        """sha256 of the prompt's full definition, recorded in each answer event.

        Ties the audit record to the exact prompt text, not just its version
        label, and lets a test catch a prompt edited in place.
        """
        data = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()


# Prompts are never edited once used. To change one, add a new version and
# point ACTIVE_PROMPT_VERSION at it; old versions stay so old answers can be
# explained.
PROMPTS: dict[tuple[str, str], Prompt] = {
    ("rag_answer", "v1"): Prompt(
        id="rag_answer",
        version="v1",
        system=(
            "You are a legal research assistant. Answer only from the numbered "
            "excerpts provided. Cite every statement with the number of the excerpt "
            "it comes from in square brackets, for example [1] or [2][3]. If the "
            "excerpts don't contain the answer, say that it can't be answered from "
            "the provided documents. Don't add facts or advice that the excerpts "
            "don't support."
        ),
        user_template="Excerpts:\n\n{context}\n\nQuestion: {question}",
    ),
}

ACTIVE_PROMPT_ID = "rag_answer"
ACTIVE_PROMPT_VERSION = "v1"


def get_prompt(prompt_id: str = ACTIVE_PROMPT_ID, version: str = ACTIVE_PROMPT_VERSION) -> Prompt:
    try:
        return PROMPTS[(prompt_id, version)]
    except KeyError:
        raise KeyError(f"unknown prompt {prompt_id}@{version}") from None
