-- B3 (27.09.2026): directory sessions, dispatcher decisions on forecast cards,
-- unsent work-order drafts and the append-only audit journal (SEC-02, SEC-03,
-- API-03, API-04). Idempotent: the loader applies every file on each run.
-- No foreign keys to tables of other packages: forecast cards live in 0013 and
-- are checked by the API, imports (0012) write here only through audit_events.

-- Server-side sessions. Only SHA-256 digests of the cookie and anti-CSRF tokens
-- are stored; roles are the directory groups resolved at login.
CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id uuid PRIMARY KEY,
    token_sha256 text NOT NULL UNIQUE CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    csrf_sha256 text NOT NULL CHECK (csrf_sha256 ~ '^[0-9a-f]{64}$'),
    subject_id text NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 256),
    display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 256),
    roles text[] NOT NULL CHECK (
        cardinality(roles) >= 1
        AND roles <@ ARRAY['dispatcher', 'analyst', 'admin']::text[]
    ),
    auth_source text NOT NULL DEFAULT 'ldap' CHECK (auth_source = 'ldap'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    CHECK (expires_at > created_at),
    CHECK (revoked_at IS NULL OR revoked_at >= created_at)
);

CREATE INDEX IF NOT EXISTS auth_sessions_subject_idx
ON auth_sessions (subject_id, created_at DESC);

CREATE INDEX IF NOT EXISTS auth_sessions_expires_idx
ON auth_sessions (expires_at);

-- Every decision is a new revision of its card; revisions are never rewritten.
CREATE TABLE IF NOT EXISTS forecast_decisions (
    decision_id uuid PRIMARY KEY,
    forecast_id text NOT NULL CHECK (length(forecast_id) BETWEEN 1 AND 256),
    revision integer NOT NULL CHECK (revision >= 1),
    decision_code text NOT NULL
      CHECK (decision_code IN ('R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R7')),
    reason_code text NOT NULL CHECK (length(reason_code) BETWEEN 1 AND 64),
    reason_text text NOT NULL CHECK (length(reason_text) BETWEEN 1 AND 300),
    verification_methods text[] NOT NULL CHECK (
        cardinality(verification_methods) >= 1
        AND verification_methods <@ ARRAY[
            'source_records', 'remote_poll', 'call_collector',
            'video', 'field_visit', 'not_checked'
        ]::text[]
    ),
    dictionary_version text NOT NULL CHECK (length(dictionary_version) >= 1),
    actor_id text NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 256),
    actor_role text NOT NULL CHECK (actor_role IN ('dispatcher', 'admin')),
    -- «Кому и когда сообщено» (contract C0.1): both or none, not after the decision.
    notified_to text CHECK (notified_to IS NULL OR length(notified_to) BETWEEN 1 AND 200),
    notified_at timestamptz,
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 128),
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    request_id text NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (forecast_id, revision),
    UNIQUE (forecast_id, idempotency_key),
    CHECK ((notified_to IS NULL) = (notified_at IS NULL)),
    CHECK (notified_at IS NULL OR notified_at <= decided_at)
);

-- R3/R4 create an internal draft. Automatic sending is not implemented (AGENTS.md):
-- the only status is «не отправлен».
CREATE TABLE IF NOT EXISTS work_order_drafts (
    draft_id uuid PRIMARY KEY,
    decision_id uuid NOT NULL UNIQUE REFERENCES forecast_decisions (decision_id),
    forecast_id text NOT NULL CHECK (length(forecast_id) BETWEEN 1 AND 256),
    decision_code text NOT NULL CHECK (decision_code IN ('R3', 'R4')),
    status text NOT NULL DEFAULT 'not_sent' CHECK (status = 'not_sent'),
    note text CHECK (note IS NULL OR length(note) <= 500),
    created_by text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 256),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS work_order_drafts_forecast_idx
ON work_order_drafts (forecast_id, created_at DESC);

-- Significant actions: login/logout, uploads (B1 hook), decisions and drafts.
-- action is "<area>.<verb>", e.g. auth.login, import.uploaded, decision.created.
CREATE TABLE IF NOT EXISTS audit_events (
    audit_id uuid PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    action text NOT NULL CHECK (action ~ '^[a-z][a-z_]*\.[a-z][a-z_]*$'),
    outcome text NOT NULL CHECK (outcome IN ('success', 'denied', 'failure')),
    actor_id text NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 256),
    actor_role text CHECK (actor_role IS NULL OR actor_role IN ('dispatcher', 'analyst', 'admin')),
    target_kind text NOT NULL CHECK (target_kind ~ '^[a-z][a-z_]*$'),
    target_id text CHECK (target_id IS NULL OR length(target_id) BETWEEN 1 AND 256),
    request_id text NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
    client_address text CHECK (client_address IS NULL OR length(client_address) <= 64),
    details jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details) = 'object')
);

CREATE INDEX IF NOT EXISTS audit_events_time_idx
ON audit_events (occurred_at DESC, audit_id);

CREATE INDEX IF NOT EXISTS audit_events_target_idx
ON audit_events (target_kind, target_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS audit_events_actor_idx
ON audit_events (actor_id, occurred_at DESC);

-- History cannot be rewritten or removed row by row (SEC-03). The API has no
-- update/delete path; this guards direct SQL mistakes as well.
CREATE OR REPLACE FUNCTION infra_pulse_reject_history_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
    USING ERRCODE = 'insufficient_privilege';
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'audit_events'::regclass AND tgname = 'audit_events_append_only'
  ) THEN
    CREATE TRIGGER audit_events_append_only
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION infra_pulse_reject_history_change();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'forecast_decisions'::regclass
      AND tgname = 'forecast_decisions_append_only'
  ) THEN
    CREATE TRIGGER forecast_decisions_append_only
    BEFORE UPDATE OR DELETE ON forecast_decisions
    FOR EACH ROW EXECUTE FUNCTION infra_pulse_reject_history_change();
  END IF;
END $$;
