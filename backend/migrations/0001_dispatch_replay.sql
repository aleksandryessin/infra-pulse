-- Initial bounded historical replay slice. It does not create live ingestion,
-- work items, authorization or forecasts.
CREATE TABLE IF NOT EXISTS dispatch_replay_snapshots (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    manifest_sha256 text NOT NULL,
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    row_count bigint NOT NULL CHECK (row_count >= 0),
    alarm_count bigint NOT NULL CHECK (alarm_count >= 0),
    loaded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_id, snapshot_id),
    CHECK (window_start < window_end)
);

CREATE TABLE IF NOT EXISTS dispatch_observations (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    row_uid text NOT NULL,
    source_event_id text,
    channel_id text NOT NULL,
    object_id text,
    sensor_type text,
    system_type text,
    value_raw text NOT NULL,
    value_numeric double precision,
    alarm boolean NOT NULL,
    event_at timestamptz NOT NULL,
    available_at timestamptz NOT NULL,
    availability_basis text NOT NULL CHECK (availability_basis = 'simulated'),
    source_file text NOT NULL,
    source_sha256 text NOT NULL,
    record_ordinal bigint NOT NULL,
    event_local_raw text NOT NULL,
    reference_version text,
    quality_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (namespace_id, snapshot_id, row_uid),
    FOREIGN KEY (namespace_id, snapshot_id)
      REFERENCES dispatch_replay_snapshots (namespace_id, snapshot_id)
);

CREATE INDEX IF NOT EXISTS dispatch_observations_queue_idx
ON dispatch_observations
(namespace_id, snapshot_id, alarm DESC, available_at DESC, row_uid);

CREATE INDEX IF NOT EXISTS dispatch_observations_channel_idx
ON dispatch_observations
(namespace_id, snapshot_id, channel_id, event_at DESC, row_uid);
