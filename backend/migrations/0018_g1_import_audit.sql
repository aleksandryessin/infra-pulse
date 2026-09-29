-- G1 (27.09.2026): journal in the ТЗ Appendix 1 layout, XLSX import report and the
-- audit of user actions. (0017 is reserved for package B4.) Idempotent: the loader
-- and the worker apply every file on each run.

-- The Appendix 1 layout has no alarm column: such records keep alarm = NULL,
-- «не передан источником» (decision of the customer technologist, 27.09) — never
-- false and never derived from the text. Dropping NOT NULL is a catalog change only.
-- Readers treat NULL as «not true»: counts and bands of source alarms take alarm =
-- true only; the watch-text band takes alarm IS NOT TRUE (backend/README, G1).
ALTER TABLE dispatch_observations ALTER COLUMN alarm DROP NOT NULL;

-- «ИД типа канала данных» of the Appendix 1 journal, verbatim. The sensor type itself
-- comes from the channel reference; a contradicting ID is quarantined at load
-- (channel_type_conflict). NULL for the organizers' export and API batches. A nullable
-- column without a default does not rewrite the observation table.
ALTER TABLE dispatch_observations
    ADD COLUMN IF NOT EXISTS source_channel_type_id text;

-- Import report (contract ImportFile): recognised header and container, records whose
-- source sent no alarm flag, and report lines shown to the administrator as written.
ALTER TABLE import_files ADD COLUMN IF NOT EXISTS source_layout text
    CHECK (source_layout IS NULL OR source_layout IN (
      'organizers_export', 'tz_appendix1', 'api_batch', 'reference'
    ));
ALTER TABLE import_files ADD COLUMN IF NOT EXISTS source_container text
    CHECK (source_container IS NULL OR source_container IN ('csv', 'xlsx', 'json'));
ALTER TABLE import_files ADD COLUMN IF NOT EXISTS alarm_not_provided bigint
    CHECK (alarm_not_provided IS NULL OR alarm_not_provided >= 0);
ALTER TABLE import_files ADD COLUMN IF NOT EXISTS notes jsonb NOT NULL DEFAULT '[]'::jsonb;

-- Quarantine reason of 0012 extended in place; re-created only when missing.
DO $$
DECLARE
  definition text;
BEGIN
  SELECT pg_get_constraintdef(oid) INTO definition FROM pg_constraint
  WHERE conrelid = 'import_quarantine'::regclass AND conname = 'import_quarantine_reason_check';
  IF definition IS NULL OR position('channel_type_conflict' IN definition) = 0 THEN
    ALTER TABLE import_quarantine DROP CONSTRAINT IF EXISTS import_quarantine_reason_check;
    ALTER TABLE import_quarantine ADD CONSTRAINT import_quarantine_reason_check CHECK (
      reason IN (
        'bad_column_count', 'bad_date', 'bad_time', 'bad_bool', 'empty_channel',
        'bad_encoding', 'value_too_long', 'channel_type_conflict'
      )
    );
  END IF;
END $$;

-- Views, aggregated polls, rejected requests and import status changes are written to
-- audit_events (0014) by the API middleware and the worker; no new table is needed.
-- The administrator's audit export filters by action and period.
CREATE INDEX IF NOT EXISTS audit_events_action_idx
ON audit_events (action, occurred_at DESC);
