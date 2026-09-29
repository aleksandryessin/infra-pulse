-- A local, bounded received-batch stream. The physical scope table retains its
-- replay name for compatibility; scope_kind distinguishes historical replay.
ALTER TABLE dispatch_replay_snapshots
  ALTER COLUMN manifest_sha256 DROP NOT NULL;
ALTER TABLE dispatch_replay_snapshots
  ADD COLUMN IF NOT EXISTS scope_kind text NOT NULL DEFAULT 'replay',
  ADD COLUMN IF NOT EXISTS last_received_at timestamptz;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'dispatch_replay_snapshots'::regclass
      AND conname = 'dispatch_replay_snapshots_scope_kind_check'
  ) THEN
    ALTER TABLE dispatch_replay_snapshots
      ADD CONSTRAINT dispatch_replay_snapshots_scope_kind_check
      CHECK (scope_kind IN ('replay', 'received'));
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'dispatch_observations'::regclass
      AND conname = 'dispatch_observations_availability_basis_check'
      AND pg_get_constraintdef(oid) NOT LIKE '%observed%'
  ) THEN
    ALTER TABLE dispatch_observations
      DROP CONSTRAINT dispatch_observations_availability_basis_check;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'dispatch_observations'::regclass
      AND conname = 'dispatch_observations_availability_basis_check'
  ) THEN
    ALTER TABLE dispatch_observations
      ADD CONSTRAINT dispatch_observations_availability_basis_check
      CHECK (availability_basis IN ('simulated', 'observed'));
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS dispatch_received_batches (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    batch_id text NOT NULL,
    source_sha256 text NOT NULL,
    source_name text NOT NULL,
    received_at timestamptz NOT NULL,
    row_count integer NOT NULL CHECK (row_count > 0),
    alarm_count integer NOT NULL CHECK (alarm_count >= 0),
    PRIMARY KEY (namespace_id, snapshot_id, batch_id),
    FOREIGN KEY (namespace_id, snapshot_id)
      REFERENCES dispatch_replay_snapshots (namespace_id, snapshot_id)
);
