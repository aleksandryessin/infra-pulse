-- XML observation batches (28.09.2026, ТЗ §7 «JSON, XML»): POST /api/v1/observations
-- accepts the same batch in XML. The body is stored as sent (<import>.xml) with format
-- 'journal_json' (the API batch), and the import report names the container 'xml'.
-- Idempotent: the CHECK of 0018 is re-created only when 'xml' is missing.
DO $$
DECLARE
  definition text;
BEGIN
  SELECT pg_get_constraintdef(oid) INTO definition FROM pg_constraint
  WHERE conrelid = 'import_files'::regclass
    AND conname = 'import_files_source_container_check';
  IF definition IS NULL OR position('''xml''' IN definition) = 0 THEN
    ALTER TABLE import_files DROP CONSTRAINT IF EXISTS import_files_source_container_check;
    ALTER TABLE import_files ADD CONSTRAINT import_files_source_container_check CHECK (
      source_container IS NULL OR source_container IN ('csv', 'xlsx', 'json', 'xml')
    );
  END IF;
END $$;
