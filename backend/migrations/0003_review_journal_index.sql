-- Bounded local replay journal, newest notes first.
CREATE INDEX IF NOT EXISTS replay_review_notes_journal_idx
ON replay_review_notes (namespace_id, snapshot_id, created_at DESC, note_id DESC);
