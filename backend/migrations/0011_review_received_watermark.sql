-- Keep the committed received-list boundary seen when a local note was made.
-- Older notes have NULL: their original received view cannot be reconstructed.
ALTER TABLE replay_review_notes
  ADD COLUMN IF NOT EXISTS displayed_received_watermark bigint;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'replay_review_notes'::regclass
      AND conname = 'replay_review_notes_received_watermark_check'
  ) THEN
    ALTER TABLE replay_review_notes
      ADD CONSTRAINT replay_review_notes_received_watermark_check
      CHECK (displayed_received_watermark IS NULL OR displayed_received_watermark >= 0);
  END IF;
END $$;
