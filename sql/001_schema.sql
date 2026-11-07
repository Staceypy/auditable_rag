-- Auditable RAG schema.
--
-- Run as the superuser: once by the Postgres container on first start, and by
-- the test setup against a fresh test database. Tables are owned by the
-- superuser, so app_user only has the rights granted explicitly below.

CREATE EXTENSION IF NOT EXISTS vector;

-- Roles are cluster-wide, so the test database finds app_user already there.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user LOGIN PASSWORD 'app_user_demo_password';  -- demo only
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- Corpus
-- ---------------------------------------------------------------------------

-- One row per (title, version). Re-ingesting a changed document adds a new
-- version; old versions and their chunks stay so past answers remain traceable.
CREATE TABLE documents (
    id          BIGSERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    version     INTEGER NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (title, version)
);

CREATE TABLE chunks (
    id                BIGSERIAL PRIMARY KEY,
    document_id       BIGINT NOT NULL REFERENCES documents (id),
    document_version  INTEGER NOT NULL,
    page              INTEGER NOT NULL,
    content           TEXT NOT NULL,
    -- sha256 of content; audit events record this, not just the chunk id.
    content_hash      CHAR(64) NOT NULL,
    embedding         vector(1536) NOT NULL
);

CREATE INDEX chunks_document_idx ON chunks (document_id);

-- ---------------------------------------------------------------------------
-- Audit
-- ---------------------------------------------------------------------------

-- Full question / answer text, addressed by its sha256. Events reference blobs
-- by hash only, so a blob can be deleted (e.g. for GDPR) by a privileged role
-- without changing any event or breaking the hash chain.
CREATE TABLE audit_blobs (
    hash        CHAR(64) PRIMARY KEY,
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only, hash-chained event log.
--   hash = sha256(prev_hash || canonical_json(every column except hash))
-- The app assigns seq and created_at itself while holding a lock (app/audit.py),
-- so each value is known before hashing and seq order equals chain order.
CREATE TABLE audit_events (
    seq         BIGSERIAL PRIMARY KEY,
    event_id    UUID NOT NULL UNIQUE,
    event_type  TEXT NOT NULL
                CHECK (event_type IN ('ai.answer_generated', 'human.review', 'audit.read')),
    trace_id    TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    matter_id   TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    actor_role  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    payload     JSONB NOT NULL,
    prev_hash   CHAR(64) NOT NULL,
    hash        CHAR(64) NOT NULL UNIQUE
);

CREATE INDEX audit_events_matter_idx ON audit_events (tenant_id, matter_id, seq);

-- Second line of defence after the grants below: even a role that has UPDATE
-- or DELETE (including the superuser) is stopped by these triggers. They can
-- only be bypassed deliberately, e.g. SET session_replication_role = replica,
-- which is superuser-only and is exactly what scripts/tamper_demo.py does.
CREATE FUNCTION audit_events_block_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only: % is not allowed', TG_OP;
END
$$;

CREATE TRIGGER audit_events_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_block_mutation();

-- Row triggers don't fire on TRUNCATE, so it needs its own statement trigger.
CREATE TRIGGER audit_events_no_truncate
    BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION audit_events_block_mutation();

-- ---------------------------------------------------------------------------
-- Privileges for app_user
-- ---------------------------------------------------------------------------

REVOKE ALL ON documents, chunks, audit_blobs, audit_events FROM PUBLIC, app_user;

GRANT SELECT, INSERT ON documents, chunks TO app_user;
GRANT USAGE ON SEQUENCE documents_id_seq, chunks_id_seq TO app_user;

-- No DELETE on blobs: removing content is a privileged, out-of-band action.
GRANT SELECT, INSERT ON audit_blobs TO app_user;

-- Append and read only. UPDATE, DELETE and TRUNCATE were revoked above.
GRANT SELECT, INSERT ON audit_events TO app_user;
GRANT USAGE ON SEQUENCE audit_events_seq_seq TO app_user;
