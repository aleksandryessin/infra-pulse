-- Local replay-only review notes. This is not a customer-approved workflow or
-- authorization model. The original observation is immutable.
CREATE TABLE IF NOT EXISTS replay_review_notes (
    note_id uuid PRIMARY KEY,
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    row_uid text NOT NULL,
    revision integer NOT NULL CHECK (revision >= 1),
    idempotency_key uuid NOT NULL,
    payload_sha256 text NOT NULL,
    actor_id text NOT NULL,
    action_text text NOT NULL,
    result_text text NOT NULL,
    reason_text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (namespace_id, snapshot_id, row_uid)
      REFERENCES dispatch_observations (namespace_id, snapshot_id, row_uid),
    UNIQUE (namespace_id, snapshot_id, row_uid, revision),
    UNIQUE (namespace_id, snapshot_id, row_uid, idempotency_key)
);

CREATE INDEX IF NOT EXISTS replay_review_notes_source_idx
ON replay_review_notes (namespace_id, snapshot_id, row_uid, revision);

CREATE TABLE IF NOT EXISTS replay_review_audit (
    audit_id uuid PRIMARY KEY,
    note_id uuid NOT NULL UNIQUE REFERENCES replay_review_notes (note_id),
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    row_uid text NOT NULL,
    actor_id text NOT NULL,
    action_kind text NOT NULL CHECK (action_kind = 'review_note_created'),
    payload_sha256 text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
