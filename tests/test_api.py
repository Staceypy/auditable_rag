"""HTTP API: /ask, /review and /audit, each checked against the audit chain."""
import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app import audit, config
from app.ingest import SAMPLE_DOCS, ingest_directory
from tests.conftest import TEST_APP_URL
from tests.fakes import FakeChat, FakeEmbedder

ASK = {"question": "How many days notice is needed for a share transfer under the right of "
                    "first refusal?",
       "tenant_id": "tenant-a", "matter_id": "matter-1", "user_id": "alice", "role": "associate"}
READER = {"tenant_id": "tenant-a", "user_id": "carol", "role": "compliance"}


@pytest.fixture
def chat():
    return FakeChat()


@pytest.fixture
def client(app_conn, chat):
    embedder = FakeEmbedder()
    ingest_directory(app_conn, embedder, SAMPLE_DOCS)
    with TestClient(api_module.create_app(embedder, chat, TEST_APP_URL)) as c:
        yield c


def events(conn, event_type=None):
    rows = audit.load_chain(conn)
    return [r for r in rows if event_type is None or r["event_type"] == event_type]


def ask(client, **overrides) -> dict:
    response = client.post("/ask", json={**ASK, **overrides})
    assert response.status_code == 200, response.text
    return response.json()


def review(client, event_id, action="accepted", **extra):
    return client.post("/review", json={"event_id": event_id, "action": action,
                                        "user_id": "bob", "role": "partner", **extra})


# --- /ask ----------------------------------------------------------------------


def test_ask_returns_answer_with_citations(client):
    body = ask(client)
    assert body["answer"] == "The notice period is 45 days [1]."
    [citation] = body["citations"]
    assert citation["title"].startswith("Shareholders") and citation["page"] == 2
    assert citation["document_version"] == 2


def test_ask_writes_exactly_one_event_with_pinned_model_and_prompt(client, app_conn):
    body = ask(client)
    [event] = events(app_conn)
    assert event["event_type"] == "ai.answer_generated"
    assert str(event["event_id"]) == body["event_id"]
    assert (event["tenant_id"], event["matter_id"], event["actor_id"], event["actor_role"]) == \
        ("tenant-a", "matter-1", "alice", "associate")

    payload = event["payload"]
    assert payload["model"]["requested"] == config.CHAT_MODEL
    assert payload["model"]["params"] == config.CHAT_PARAMS
    assert payload["prompt"] == {"id": config.ACTIVE_PROMPT_ID,
                                 "version": config.ACTIVE_PROMPT_VERSION,
                                 "hash": config.get_prompt().hash}
    assert len(payload["retrieval"]["chunks"]) == config.TOP_K


def test_ask_stores_question_and_answer_as_blobs(client, app_conn):
    body = ask(client)
    payload = events(app_conn)[0]["payload"]
    blobs = audit.load_blobs(app_conn)
    assert blobs[payload["query_hash"]] == ASK["question"]
    assert blobs[payload["output_hash"]] == body["answer"]


def test_ask_uses_trace_id_header(client, app_conn):
    response = client.post("/ask", json=ASK, headers={"X-Trace-Id": "trace-123"})
    assert response.json()["trace_id"] == "trace-123"
    assert events(app_conn)[0]["trace_id"] == "trace-123"


def test_ask_fails_closed_when_audit_write_fails(client, app_conn, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr(api_module.audit, "append_event", broken)
    response = client.post("/ask", json=ASK)
    assert response.status_code == 503
    assert "answer" not in response.json()


def test_ask_model_error_returns_502_and_writes_nothing(client, app_conn, chat):
    def fail(messages):
        raise TimeoutError("model timed out")

    chat.reply = fail
    assert client.post("/ask", json=ASK).status_code == 502
    assert events(app_conn) == []


@pytest.mark.parametrize("change", [{"question": ""}, {"tenant_id": ""}, {"role": None}])
def test_ask_validates_input(client, change):
    assert client.post("/ask", json={**ASK, **change}).status_code == 422


# --- /review ---------------------------------------------------------------------


def test_review_appends_event_and_leaves_original_unchanged(client, app_conn):
    answer_id = ask(client)["event_id"]
    [before] = events(app_conn)

    response = review(client, answer_id)
    assert response.status_code == 200, response.text

    original, review_event = events(app_conn)
    assert original == before  # every column, including its hash
    assert review_event["event_type"] == "human.review"
    assert review_event["payload"] == {"ref_event_id": answer_id,
                                       "ref_event_hash": before["hash"], "action": "accepted"}
    # Review is filed under the answer's tenant and matter, and linked to its trace.
    for column in ("tenant_id", "matter_id", "trace_id"):
        assert review_event[column] == before[column]
    assert (review_event["actor_id"], review_event["actor_role"]) == ("bob", "partner")


def test_edited_review_stores_edited_answer_as_blob(client, app_conn):
    answer_id = ask(client)["event_id"]
    edited = "The notice period is 45 days (clause 3) [1]."
    assert review(client, answer_id, "edited", edited_answer=edited).status_code == 200

    payload = events(app_conn, "human.review")[0]["payload"]
    assert payload["edited_output_hash"] == audit.sha256_text(edited)
    assert audit.load_blobs(app_conn)[payload["edited_output_hash"]] == edited


@pytest.mark.parametrize("action, extra", [
    ("edited", {}),                                 # edited needs the new text
    ("accepted", {"edited_answer": "something"}),  # only edited may have it
    ("approved", {}),                               # unknown action
])
def test_review_validates_action_and_edited_answer(client, action, extra):
    answer_id = ask(client)["event_id"]
    assert review(client, answer_id, action, **extra).status_code == 422


def test_review_of_unknown_or_non_answer_event_is_404(client):
    assert review(client, "00000000-0000-0000-0000-000000000000").status_code == 404
    answer_id = ask(client)["event_id"]
    review_id = review(client, answer_id).json()["event_id"]
    assert review(client, review_id).status_code == 404  # can't review a review


def test_answer_can_have_several_reviews(client, app_conn):
    answer_id = ask(client)["event_id"]
    review(client, answer_id, "rejected")
    review(client, answer_id, "accepted")
    assert [e["payload"]["action"] for e in events(app_conn, "human.review")] == \
        ["rejected", "accepted"]


# --- /audit ------------------------------------------------------------------------


def test_audit_timeline_joins_answers_with_reviews(client):
    first = ask(client)["event_id"]
    second = ask(client, question="What law governs the NDA?")["event_id"]
    review(client, first, "edited", edited_answer="Corrected answer [1].")

    body = client.get("/audit/matter-1", params=READER).json()
    assert [a["event_id"] for a in body["answers"]] == [first, second]

    a1, a2 = body["answers"]
    assert a1["question"] == ASK["question"]
    assert a1["answer"] == "The notice period is 45 days [1]."
    assert a1["model"]["requested"] == config.CHAT_MODEL
    assert a1["prompt"]["version"] == config.ACTIVE_PROMPT_VERSION
    assert len(a1["cited_chunks"]) == 1
    assert [(r["action"], r["edited_answer"]) for r in a1["reviews"]] == \
        [("edited", "Corrected answer [1].")]
    assert a2["reviews"] == []


def test_every_audit_read_is_logged(client, app_conn):
    ask(client)
    first = client.get("/audit/matter-1", params=READER).json()
    second = client.get("/audit/matter-1", params={**READER, "user_id": "dave"}).json()

    reads = events(app_conn, "audit.read")
    assert [str(r["event_id"]) for r in reads] == [first["read_event_id"], second["read_event_id"]]
    assert [r["actor_id"] for r in reads] == ["carol", "dave"]
    assert reads[0]["payload"] == {"returned_events": 1, "up_to_seq": 1}

    # The first read shows up in the second read's access log.
    assert [r["actor_id"] for r in second["reads"]] == ["carol"]


def test_audit_is_scoped_to_tenant_and_matter(client):
    ask(client)
    ask(client, matter_id="matter-2")
    ask(client, tenant_id="tenant-b")  # same matter id, other tenant

    body = client.get("/audit/matter-1", params=READER).json()
    assert len(body["answers"]) == 1


def test_audit_requires_reader_identity(client):
    assert client.get("/audit/matter-1", params={"tenant_id": "tenant-a"}).status_code == 422


def test_audit_shows_redacted_text_as_none(client, app_conn, super_conn):
    ask(client)
    payload = events(app_conn)[0]["payload"]
    super_conn.execute("DELETE FROM audit_blobs WHERE hash = %s", (payload["query_hash"],))

    [answer] = client.get("/audit/matter-1", params=READER).json()["answers"]
    assert answer["question"] is None
    assert answer["answer"] is not None


def test_chain_stays_valid_across_all_endpoints(client, app_conn):
    answer_id = ask(client)["event_id"]
    review(client, answer_id, "edited", edited_answer="Better [1].")
    client.get("/audit/matter-1", params=READER)

    result = audit.verify_database(app_conn)
    assert result.ok, result.reason
    assert [e["event_type"] for e in events(app_conn)] == \
        ["ai.answer_generated", "human.review", "audit.read"]
