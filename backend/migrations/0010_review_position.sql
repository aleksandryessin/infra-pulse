-- Stable pagination for locally persisted review notes. The count and note
-- position advance in the same transaction as the note and audit row.
ALTER TABLE dispatch_replay_snapshots
  ADD COLUMN IF NOT EXISTS review_count bigint NOT NULL DEFAULT 0;

ALTER TABLE replay_review_notes
  ADD COLUMN IF NOT EXISTS review_position bigint;

WITH ranked AS (
  SELECT namespace_id, snapshot_id, note_id,
         row_number() OVER (
           PARTITION BY namespace_id, snapshot_id
           ORDER BY created_at, note_id
         ) AS position
  FROM replay_review_notes
)
UPDATE replay_review_notes AS note
SET review_position = ranked.position
FROM ranked
WHERE note.namespace_id = ranked.namespace_id
  AND note.snapshot_id = ranked.snapshot_id
  AND note.note_id = ranked.note_id
  AND note.review_position IS NULL;

WITH totals AS (
  SELECT namespace_id, snapshot_id, count(*) AS note_count
  FROM replay_review_notes
  GROUP BY namespace_id, snapshot_id
)
UPDATE dispatch_replay_snapshots AS scope
SET review_count = totals.note_count
FROM totals
WHERE scope.namespace_id = totals.namespace_id
  AND scope.snapshot_id = totals.snapshot_id
  AND scope.review_count < totals.note_count;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'replay_review_notes'::regclass
      AND conname = 'replay_review_notes_position_check'
  ) THEN
    ALTER TABLE replay_review_notes
      ADD CONSTRAINT replay_review_notes_position_check
      CHECK (review_position IS NOT NULL AND review_position > 0);
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS replay_review_notes_position_idx
ON replay_review_notes (namespace_id, snapshot_id, review_position);
