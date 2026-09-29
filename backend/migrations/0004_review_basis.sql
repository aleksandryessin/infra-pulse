-- Preserve the basis visible when a local replay review was recorded.
-- Existing prototype notes predate this field and retain NULL provenance.
ALTER TABLE replay_review_notes
  ADD COLUMN IF NOT EXISTS view_as_of timestamptz,
  ADD COLUMN IF NOT EXISTS policy_version text,
  ADD COLUMN IF NOT EXISTS attention_band text,
  ADD COLUMN IF NOT EXISTS reason_codes jsonb;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'replay_review_notes'::regclass
      AND conname = 'replay_review_notes_attention_band_check'
  ) THEN
    ALTER TABLE replay_review_notes
      ADD CONSTRAINT replay_review_notes_attention_band_check
      CHECK (attention_band IN ('source_alarm', 'watch_text', 'chronological'));
  END IF;
END $$;
