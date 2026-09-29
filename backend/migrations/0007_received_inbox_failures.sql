-- Invalid local inbox files remain visible until replaced by a valid batch.
-- File contents and parser exception text are never exposed by the API.
ALTER TABLE dispatch_replay_snapshots
  ADD COLUMN IF NOT EXISTS last_scanned_at timestamptz;

CREATE TABLE IF NOT EXISTS dispatch_received_inbox_failures (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    source_name text NOT NULL,
    error_kind text NOT NULL CHECK (
      error_kind IN ('invalid_batch', 'batch_conflict', 'file_unreadable')
    ),
    detected_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (namespace_id, snapshot_id, source_name),
    FOREIGN KEY (namespace_id, snapshot_id)
      REFERENCES dispatch_replay_snapshots (namespace_id, snapshot_id)
);
