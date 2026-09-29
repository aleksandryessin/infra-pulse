-- B1 (27.09.2026): uploaded source files, their quarantine, versioned references
-- and the PostgreSQL job queue of the ingestion worker. HTTP only inserts
-- import_files + jobs; the worker parses, loads dispatch_observations and calls
-- the forecast recompute. No foreign keys to B2/B3 tables (0013/0014).
-- The upload is also written to audit_events (0014, "import.uploaded") in the same
-- HTTP transaction (ingestion/imports_pg.create_import); import_files.uploaded_by
-- keeps the author for the import report.

CREATE TABLE IF NOT EXISTS import_files (
    seq bigserial NOT NULL UNIQUE,
    import_id text PRIMARY KEY CHECK (length(import_id) BETWEEN 1 AND 128),
    format text NOT NULL CHECK (format IN (
      'journal_csv', 'reference_channels_csv',
      'reference_objects_csv', 'reference_states_csv'
    )),
    file_name text NOT NULL CHECK (length(file_name) BETWEEN 1 AND 255),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes BETWEEN 0 AND 52428800),
    -- File name inside the shared upload directory; never a path from the client.
    stored_name text NOT NULL,
    uploaded_by text NOT NULL CHECK (length(uploaded_by) >= 1),
    uploaded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    status text NOT NULL CHECK (status IN (
      'queued', 'parsing', 'imported', 'recomputing',
      'published', 'duplicate', 'failed'
    )),
    finished_at timestamptz,
    rows_total bigint CHECK (rows_total >= 0),
    rows_accepted bigint CHECK (rows_accepted >= 0),
    rows_duplicate bigint CHECK (rows_duplicate >= 0),
    rows_quarantined bigint CHECK (rows_quarantined >= 0),
    unknown_channels integer CHECK (unknown_channels >= 0),
    -- Repeated header lines inside the file: not records, kept for the audit only.
    technical_headers integer CHECK (technical_headers >= 0),
    event_from timestamptz,
    event_to timestamptz,
    reference_version text,
    duplicate_of text REFERENCES import_files (import_id),
    forecast_generation integer CHECK (forecast_generation >= 1),
    error_code text CHECK (error_code IN (
      'file_too_large', 'unknown_format', 'bad_header', 'bad_encoding',
      'no_valid_rows', 'reference_missing', 'recompute_failed', 'internal_error'
    )),
    -- Short diagnostic for administrators (exception class, never file content).
    error_detail text,
    timings jsonb NOT NULL DEFAULT '[]'::jsonb,
    quarantine_reasons jsonb NOT NULL DEFAULT '{}'::jsonb,
    new_card_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    released_card_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- Observation scope the worker wrote into (journal only).
    target_namespace text,
    target_stream text,
    CHECK ((status = 'failed') = (error_code IS NOT NULL)),
    CHECK ((status IN ('published', 'duplicate', 'failed')) = (finished_at IS NOT NULL)),
    CHECK ((status = 'duplicate') = (duplicate_of IS NOT NULL)),
    CHECK (event_from IS NULL OR event_to IS NULL OR event_from <= event_to)
);

CREATE INDEX IF NOT EXISTS import_files_sha_idx ON import_files (format, sha256, seq);

CREATE TABLE IF NOT EXISTS import_quarantine (
    import_id text NOT NULL REFERENCES import_files (import_id),
    line_no bigint NOT NULL CHECK (line_no >= 1),
    record_ordinal bigint NOT NULL CHECK (record_ordinal >= 1),
    reason text NOT NULL CHECK (reason IN (
      'bad_column_count', 'bad_date', 'bad_time', 'bad_bool',
      'empty_channel', 'bad_encoding', 'value_too_long'
    )),
    -- Parsed cells; undecodable bytes are escaped, never dropped.
    raw_cells jsonb NOT NULL,
    PRIMARY KEY (import_id, line_no)
);

-- One row per accepted reference file. The newest activation of a kind is the
-- current mapping for later journal loads; earlier observations keep the
-- reference_version they were loaded with (no retroactive rewrite).
CREATE TABLE IF NOT EXISTS ref_versions (
    version_id text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('channels', 'objects', 'states')),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    import_id text NOT NULL REFERENCES import_files (import_id),
    row_count integer NOT NULL CHECK (row_count >= 0),
    activation_seq bigserial NOT NULL UNIQUE,
    activated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (kind, sha256)
);

CREATE INDEX IF NOT EXISTS ref_versions_active_idx ON ref_versions (kind, activation_seq DESC);

-- Reference rows are stored verbatim by source line. A channel listed twice with
-- different content stays ambiguous and is not used for object mapping.
CREATE TABLE IF NOT EXISTS ref_channels (
    version_id text NOT NULL REFERENCES ref_versions (version_id),
    line_no bigint NOT NULL,
    channel_id text NOT NULL,
    system_type text NOT NULL,
    sensor_type text NOT NULL,
    tag text NOT NULL,
    name text NOT NULL,
    object_id text,
    PRIMARY KEY (version_id, line_no)
);

CREATE INDEX IF NOT EXISTS ref_channels_channel_idx ON ref_channels (version_id, channel_id);

CREATE TABLE IF NOT EXISTS ref_objects (
    version_id text NOT NULL REFERENCES ref_versions (version_id),
    line_no bigint NOT NULL,
    object_id text NOT NULL,
    level_raw text NOT NULL,
    parent_raw text NOT NULL,
    object_kind text NOT NULL,
    name text NOT NULL,
    PRIMARY KEY (version_id, line_no)
);

CREATE INDEX IF NOT EXISTS ref_objects_object_idx ON ref_objects (version_id, object_id);

CREATE TABLE IF NOT EXISTS ref_states (
    version_id text NOT NULL REFERENCES ref_versions (version_id),
    line_no bigint NOT NULL,
    sensor_type text NOT NULL,
    state_set_id text NOT NULL,
    state_name text NOT NULL,
    alarm boolean NOT NULL,
    PRIMARY KEY (version_id, line_no)
);

-- Ingestion queue: claimed with FOR UPDATE SKIP LOCKED under a lease. A job whose
-- lease expired (worker crashed or restarted) is claimed again and restarts from
-- the last committed import status; the load itself is one transaction.
CREATE TABLE IF NOT EXISTS jobs (
    job_id bigserial PRIMARY KEY,
    kind text NOT NULL CHECK (kind = 'import'),
    import_id text NOT NULL UNIQUE REFERENCES import_files (import_id),
    state text NOT NULL CHECK (state IN ('queued', 'running', 'done', 'failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    run_after timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_owner text,
    lease_expires_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((state = 'running') = (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS jobs_pending_idx ON jobs (job_id) WHERE state IN ('queued', 'running');
