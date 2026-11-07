"""HTTP API. Every endpoint writes to the audit chain.

    uv run uvicorn app.api:app --reload

POST /ask                answer a question, log ai.answer_generated
POST /review             accept / edit / reject an answer, log human.review
GET  /audit/{matter_id}  timeline of answers with their reviews, log audit.read

The API fails closed: if the audit event can't be written, the caller gets an
error, not an unaudited answer or an unlogged read.

There's no authentication in this demo: user_id and role are taken from the
request as given (see README, "Known limitations").
"""
from __future__ import annotations

import logging
import uuid
from typing import Annotated, Iterator, Literal
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from app import audit, db
from app.answer import ChatClient, OpenAIChat, generate_answer
from app.embeddings import Embedder, OpenAIEmbedder

log = logging.getLogger(__name__)

NonEmpty = Annotated[str, Field(min_length=1, max_length=200)]


# ---------------------------------------------------------------------------
# Request / response bodies
# ---------------------------------------------------------------------------


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    tenant_id: NonEmpty
    matter_id: NonEmpty
    user_id: NonEmpty
    role: NonEmpty


class AskResponse(BaseModel):
    event_id: UUID
    trace_id: str
    answer: str
    citations: list[dict]


class ReviewRequest(BaseModel):
    event_id: UUID
    action: Literal["accepted", "edited", "rejected"]
    edited_answer: str | None = Field(default=None, min_length=1, max_length=20000)
    user_id: NonEmpty
    role: NonEmpty

    @model_validator(mode="after")
    def _edited_answer_only_when_edited(self):
        if (self.action == "edited") != (self.edited_answer is not None):
            raise ValueError("edited_answer is required for action 'edited' and not allowed otherwise")
        return self


class ReviewResponse(BaseModel):
    event_id: UUID
    ref_event_id: UUID


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app(
    embedder: Embedder | None = None,
    chat: ChatClient | None = None,
    database_url: str | None = None,
) -> FastAPI:
    """Build the app. Tests pass fakes and the test database URL."""
    embedder = embedder or OpenAIEmbedder()
    chat = chat or OpenAIChat()
    api = FastAPI(title="Auditable RAG demo")

    def get_conn() -> Iterator[psycopg.Connection]:
        with db.connect(database_url) as conn:
            yield conn

    def append_or_fail(conn: psycopg.Connection, **event) -> dict:
        """Write the audit event; if that fails, nothing is returned to the caller."""
        try:
            return audit.append_event(conn, **event)
        except Exception:
            log.exception("audit write failed")
            raise HTTPException(503, "audit log unavailable; nothing was returned") from None

    @api.post("/ask", response_model=AskResponse)
    def ask(
        body: AskRequest,
        conn: psycopg.Connection = Depends(get_conn),
        x_trace_id: str | None = Header(default=None, max_length=200),
    ) -> AskResponse:
        # The trace id links this audit event to observability traces (e.g.
        # LangSmith) without mixing the two systems; see README.
        trace_id = x_trace_id or str(uuid.uuid4())
        try:
            answer = generate_answer(conn, embedder, chat, body.question)
        except Exception:
            log.exception("answer generation failed")
            raise HTTPException(502, "answer generation failed") from None

        event = append_or_fail(
            conn,
            event_type="ai.answer_generated",
            trace_id=trace_id,
            tenant_id=body.tenant_id,
            matter_id=body.matter_id,
            actor_id=body.user_id,
            actor_role=body.role,
            payload={
                # Full text goes in audit_blobs; the event holds only hashes.
                "query_hash": audit.sha256_text(body.question),
                "output_hash": audit.sha256_text(answer.text),
                **answer.audit_fields(),
            },
            blobs=[body.question, answer.text],
        )
        return AskResponse(event_id=event["event_id"], trace_id=trace_id,
                           answer=answer.text, citations=answer.citations_for_user())

    @api.post("/review", response_model=ReviewResponse)
    def review(body: ReviewRequest, conn: psycopg.Connection = Depends(get_conn)) -> ReviewResponse:
        original = conn.execute(
            "SELECT event_id, event_type, trace_id, tenant_id, matter_id, hash "
            "FROM audit_events WHERE event_id = %s", (body.event_id,)
        ).fetchone()
        if original is None or original["event_type"] != "ai.answer_generated":
            raise HTTPException(404, "no AI answer with this event_id")

        payload = {
            "ref_event_id": str(original["event_id"]),
            # Also pin the reviewed event's hash, so the review says exactly
            # which version of the record it refers to.
            "ref_event_hash": original["hash"],
            "action": body.action,
        }
        blobs = []
        if body.edited_answer is not None:
            payload["edited_output_hash"] = audit.sha256_text(body.edited_answer)
            blobs.append(body.edited_answer)

        # A review is a new event. The original answer event is never updated.
        event = append_or_fail(
            conn,
            event_type="human.review",
            trace_id=original["trace_id"],
            tenant_id=original["tenant_id"],
            matter_id=original["matter_id"],
            actor_id=body.user_id,
            actor_role=body.role,
            payload=payload,
            blobs=blobs,
        )
        return ReviewResponse(event_id=event["event_id"], ref_event_id=original["event_id"])

    @api.get("/audit/{matter_id}")
    def audit_timeline(
        matter_id: str,
        tenant_id: str = Query(min_length=1, max_length=200),
        user_id: str = Query(min_length=1, max_length=200),
        role: str = Query(min_length=1, max_length=200),
        conn: psycopg.Connection = Depends(get_conn),
    ) -> dict:
        events = conn.execute(
            "SELECT * FROM audit_events WHERE tenant_id = %s AND matter_id = %s ORDER BY seq",
            (tenant_id, matter_id),
        ).fetchall()
        blobs = _load_blobs(conn, events)
        timeline = _build_timeline(events, blobs)

        # Reading the audit trail is itself audited. The read event records
        # what was returned: this matter's events up to `up_to_seq`.
        read = append_or_fail(
            conn,
            event_type="audit.read",
            tenant_id=tenant_id,
            matter_id=matter_id,
            actor_id=user_id,
            actor_role=role,
            payload={"returned_events": len(events),
                     "up_to_seq": events[-1]["seq"] if events else None},
        )
        return {"tenant_id": tenant_id, "matter_id": matter_id,
                "read_event_id": str(read["event_id"]), **timeline}

    return api


# ---------------------------------------------------------------------------
# Timeline helpers
# ---------------------------------------------------------------------------


def _load_blobs(conn: psycopg.Connection, events: list[dict]) -> dict[str, str]:
    hashes = sorted({e["payload"][f] for e in events for f in audit.BLOB_FIELDS
                     if f in e["payload"]})
    if not hashes:
        return {}
    rows = conn.execute("SELECT hash, content FROM audit_blobs WHERE hash = ANY(%s)", (hashes,))
    return {r["hash"]: r["content"] for r in rows}


def _base(event: dict) -> dict:
    return {"event_id": str(event["event_id"]), "seq": event["seq"],
            "created_at": event["created_at"].isoformat(), "actor_id": event["actor_id"],
            "actor_role": event["actor_role"], "hash": event["hash"]}


def _build_timeline(events: list[dict], blobs: dict[str, str]) -> dict:
    """Answers in order, each with its reviews; reads listed separately.

    A text that was redacted (its blob deleted) is returned as None; the
    event and its hashes are still there.
    """
    answers: dict[str, dict] = {}
    reads: list[dict] = []
    for event in events:
        payload = event["payload"]
        if event["event_type"] == "ai.answer_generated":
            answers[str(event["event_id"])] = {
                **_base(event),
                "question": blobs.get(payload["query_hash"]),
                "answer": blobs.get(payload["output_hash"]),
                "model": payload["model"],
                "prompt": payload["prompt"],
                "cited_chunks": [c for c in payload["retrieval"]["chunks"] if c["cited"]],
                "invalid_citation_markers": payload["invalid_citation_markers"],
                "reviews": [],
            }
        elif event["event_type"] == "human.review":
            edited = payload.get("edited_output_hash")
            answers[payload["ref_event_id"]]["reviews"].append({
                **_base(event),
                "action": payload["action"],
                "edited_answer": blobs.get(edited) if edited else None,
            })
        else:  # audit.read
            reads.append({**_base(event), "returned_events": payload["returned_events"]})
    return {"answers": list(answers.values()), "reads": reads}


app = create_app()
