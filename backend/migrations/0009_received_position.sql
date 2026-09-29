-- Committed import order for stable received pagination. Historical replay
-- observations keep NULL; existing local received rows are numbered once.
ALTER TABLE dispatch_observations
  ADD COLUMN IF NOT EXISTS received_position bigint;

WITH ranked AS (
  SELECT observation.namespace_id, observation.snapshot_id, observation.row_uid,
         row_number() OVER (
           PARTITION BY observation.namespace_id, observation.snapshot_id
           ORDER BY observation.available_at, observation.source_sha256,
                    observation.record_ordinal, observation.row_uid
         ) AS position
  FROM dispatch_observations AS observation
  JOIN dispatch_replay_snapshots AS scope
    ON scope.namespace_id = observation.namespace_id
   AND scope.snapshot_id = observation.snapshot_id
  WHERE scope.scope_kind = 'received'
)
UPDATE dispatch_observations AS observation
SET received_position = ranked.position
FROM ranked
WHERE observation.namespace_id = ranked.namespace_id
  AND observation.snapshot_id = ranked.snapshot_id
  AND observation.row_uid = ranked.row_uid
  AND observation.received_position IS NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'dispatch_observations'::regclass
      AND conname = 'dispatch_observations_received_position_check'
  ) THEN
    ALTER TABLE dispatch_observations
      ADD CONSTRAINT dispatch_observations_received_position_check
      CHECK ((availability_basis = 'observed') = (received_position IS NOT NULL));
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS dispatch_observations_received_position_idx
ON dispatch_observations (namespace_id, snapshot_id, received_position)
WHERE received_position IS NOT NULL;
