-- Current reference roster for coverage in a bounded historical replay.
-- Membership is a reference snapshot, not proven historical topology.
CREATE TABLE IF NOT EXISTS dispatch_replay_channel_roster (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    channel_id text NOT NULL,
    object_id text,
    system_type text,
    sensor_type text,
    reference_sha256 text NOT NULL,
    PRIMARY KEY (namespace_id, snapshot_id, channel_id),
    FOREIGN KEY (namespace_id, snapshot_id)
      REFERENCES dispatch_replay_snapshots (namespace_id, snapshot_id)
);

CREATE INDEX IF NOT EXISTS dispatch_replay_channel_roster_object_idx
ON dispatch_replay_channel_roster
(namespace_id, snapshot_id, object_id, system_type, channel_id);
