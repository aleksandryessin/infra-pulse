-- B1x (27.09.2026): integration tokens and the batch register of the observation
-- API (POST /api/v1/observations). A batch is stored as a file and becomes an
-- import_files row with format 'journal_json'; the B1 worker parses and loads it
-- exactly like a journal CSV. Idempotent: the loader applies every file on each run.

-- Bearer tokens of external systems. Only the SHA-256 of the token is stored; the
-- token itself is printed once by `python -m infra_pulse_backend.admin token create`.
-- Several live tokens may share a name (rotation); the name is the API identity
-- «integration:<name>» and the idempotency namespace of its batches.
CREATE TABLE IF NOT EXISTS integration_tokens (
    token_id text PRIMARY KEY CHECK (token_id ~ '^tok-[0-9a-f]{12}$'),
    name text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
    token_sha256 text NOT NULL UNIQUE CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    created_by text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 256),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    revoked_at timestamptz,
    revoked_by text CHECK (revoked_by IS NULL OR length(revoked_by) BETWEEN 1 AND 256),
    last_used_at timestamptz,
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL)),
    CHECK (revoked_at IS NULL OR revoked_at >= created_at)
);

CREATE INDEX IF NOT EXISTS integration_tokens_name_idx
ON integration_tokens (name, created_at DESC);

-- One row per accepted batch: (client, batch_id) is the client's idempotency key.
-- The same key with the same bytes returns the stored import; other bytes are 409.
-- client_id is the caller identity («integration:<name>» or an administrator), so a
-- retry after token rotation stays idempotent. token_id is the token that sent it.
CREATE TABLE IF NOT EXISTS observation_batches (
    client_id text NOT NULL CHECK (length(client_id) BETWEEN 1 AND 256),
    batch_id text NOT NULL CHECK (length(batch_id) BETWEEN 1 AND 128),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    import_id text NOT NULL UNIQUE REFERENCES import_files (import_id),
    token_id text REFERENCES integration_tokens (token_id),
    records integer NOT NULL CHECK (records >= 1),
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (client_id, batch_id)
);

CREATE INDEX IF NOT EXISTS observation_batches_token_idx
ON observation_batches (token_id, received_at DESC);

-- Column checks of 0012/0014 extended in place: the JSON batch format and the
-- integration role as an audit actor. Re-created only when the value is missing.
DO $$
DECLARE
  definition text;
BEGIN
  SELECT pg_get_constraintdef(oid) INTO definition FROM pg_constraint
  WHERE conrelid = 'import_files'::regclass AND conname = 'import_files_format_check';
  IF definition IS NULL OR position('journal_json' IN definition) = 0 THEN
    ALTER TABLE import_files DROP CONSTRAINT IF EXISTS import_files_format_check;
    ALTER TABLE import_files ADD CONSTRAINT import_files_format_check CHECK (format IN (
      'journal_csv', 'journal_json', 'reference_channels_csv',
      'reference_objects_csv', 'reference_states_csv'
    ));
  END IF;

  SELECT pg_get_constraintdef(oid) INTO definition FROM pg_constraint
  WHERE conrelid = 'audit_events'::regclass AND conname = 'audit_events_actor_role_check';
  IF definition IS NULL OR position('integration' IN definition) = 0 THEN
    ALTER TABLE audit_events DROP CONSTRAINT IF EXISTS audit_events_actor_role_check;
    ALTER TABLE audit_events ADD CONSTRAINT audit_events_actor_role_check CHECK (
      actor_role IS NULL
      OR actor_role IN ('dispatcher', 'analyst', 'admin', 'integration')
    );
  END IF;
END $$;
