-- B2 (27.09.2026): «обесточивание фидеров объекта» — causal phase episode detector state,
-- episodes and events, channel layout (picket, feeder kind), coverage, publication runs,
-- immutable card snapshots, outcomes and window events. Every forecast table is scoped by
-- the observation scope (namespace_id, snapshot_id) it was computed from. No foreign keys
-- to the tables of B1 (0012) or B3 (0014). Re-applying the file is a no-op.

CREATE TABLE IF NOT EXISTS forecast_scopes (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('replay', 'received')),
    generation integer NOT NULL DEFAULT 0 CHECK (generation >= 0),
    -- Last received_position of dispatch_observations read by the detector.
    detector_position bigint NOT NULL DEFAULT 0 CHECK (detector_position >= 0),
    -- First cutoff of the list (NULL: the first cutoff after the first record).
    list_from date,
    last_cutoff timestamptz,
    data_as_of timestamptz,
    next_journal_position bigint NOT NULL DEFAULT 1 CHECK (next_journal_position >= 1),
    PRIMARY KEY (namespace_id, snapshot_id)
);

-- Channel reference as the scheme needs it. Names are kept verbatim; role, feeder kind and
-- picket are derived by the versioned rules of infra_pulse_core.features.channel_names.
CREATE TABLE IF NOT EXISTS forecast_channel_layout (
    channel_id text PRIMARY KEY,
    object_id text,
    sensor_type text,
    system_type text,
    name text,
    tag text,
    role text CHECK (role IN ('feeder', 'landmark')),
    feeder_kind text CHECK (feeder_kind IN ('lighting', 'ventilation', 'pumps', 'ozk', 'other')),
    landmark_kind text CHECK (landmark_kind IN ('input', 'ats', 'panel', 'other')),
    picket_form text NOT NULL CHECK (picket_form IN ('point', 'range', 'unknown')),
    picket_from double precision CHECK (picket_from >= 0),
    picket_to double precision CHECK (picket_to >= 0),
    picket_basis text CHECK (picket_basis IN ('reference', 'channel_name', 'synthetic')),
    layout_version text NOT NULL,
    reference_version text,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((picket_form = 'unknown') = (picket_from IS NULL)),
    CHECK ((picket_form = 'unknown') = (picket_basis IS NULL)),
    CHECK ((picket_form = 'range') = (picket_to IS NOT NULL)),
    CHECK (picket_to IS NULL OR picket_to >= picket_from),
    CHECK ((role = 'landmark') = (landmark_kind IS NOT NULL)),
    CHECK ((role = 'feeder') = (feeder_kind IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS forecast_channel_layout_object_idx
ON forecast_channel_layout (object_id, sensor_type, channel_id);

CREATE TABLE IF NOT EXISTS forecast_objects (
    object_id text PRIMARY KEY,
    object_name text,
    reference_version text
);

CREATE TABLE IF NOT EXISTS forecast_detector_state (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    channel_id text NOT NULL,
    object_id text,
    first_seen timestamptz,
    last_ts timestamptz,
    state jsonb NOT NULL,
    detector_version text NOT NULL,
    PRIMARY KEY (namespace_id, snapshot_id, channel_id)
);

CREATE TABLE IF NOT EXISTS forecast_phase_episodes (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    channel_id text NOT NULL,
    start_at timestamptz NOT NULL,
    object_id text,
    end_at timestamptz,
    start_alarm boolean NOT NULL,
    power_off_at timestamptz,
    detector_version text NOT NULL,
    PRIMARY KEY (namespace_id, snapshot_id, channel_id, start_at),
    CHECK (end_at IS NULL OR end_at > start_at)
);

CREATE INDEX IF NOT EXISTS forecast_phase_episodes_object_idx
ON forecast_phase_episodes (namespace_id, snapshot_id, object_id, start_at);

-- Candidate timestamps («Неисправен»): the Q shadow of a channel at a cutoff.
CREATE TABLE IF NOT EXISTS forecast_phase_candidates (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    channel_id text NOT NULL,
    candidate_at timestamptz NOT NULL,
    PRIMARY KEY (namespace_id, snapshot_id, channel_id, candidate_at)
);

-- Events: W = 10 min chains of episode starts of one object.
CREATE TABLE IF NOT EXISTS forecast_phase_events (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    event_id text NOT NULL,
    object_id text NOT NULL,
    start_at timestamptz NOT NULL,
    last_start_at timestamptz NOT NULL,
    size integer NOT NULL CHECK (size >= 1),
    channel_ids jsonb NOT NULL,
    episode_starts jsonb NOT NULL,
    PRIMARY KEY (namespace_id, snapshot_id, event_id),
    CHECK (last_start_at >= start_at)
);

CREATE INDEX IF NOT EXISTS forecast_phase_events_object_idx
ON forecast_phase_events (namespace_id, snapshot_id, object_id, start_at);

-- Calendar days (MSK) with data; policy days are excluded periods of the working layer.
CREATE TABLE IF NOT EXISTS forecast_coverage_days (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    day date NOT NULL,
    covered boolean NOT NULL,
    policy boolean NOT NULL DEFAULT false,
    source text NOT NULL,
    PRIMARY KEY (namespace_id, snapshot_id, day)
);

-- Last phase record of an object per day: freshness of the card at a cutoff.
CREATE TABLE IF NOT EXISTS forecast_object_days (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    object_id text NOT NULL,
    day date NOT NULL,
    last_record_at timestamptz NOT NULL,
    records bigint NOT NULL CHECK (records >= 1),
    PRIMARY KEY (namespace_id, snapshot_id, object_id, day)
);

-- One run per recompute: generation + 1 in one transaction.
CREATE TABLE IF NOT EXISTS forecast_runs (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    generation integer NOT NULL CHECK (generation >= 1),
    run_id text NOT NULL,
    import_id text,
    data_as_of timestamptz NOT NULL,
    first_cutoff timestamptz,
    last_cutoff timestamptz,
    cutoffs integer NOT NULL CHECK (cutoffs >= 0),
    new_card_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    released_card_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    records_read bigint NOT NULL DEFAULT 0,
    late_channels integer NOT NULL DEFAULT 0,
    timings jsonb NOT NULL DEFAULT '{}'::jsonb,
    versions jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (namespace_id, snapshot_id, generation),
    UNIQUE (namespace_id, snapshot_id, run_id)
);

-- Every processed 00:00 MSK cutoff and the run that issued it.
CREATE TABLE IF NOT EXISTS forecast_cutoffs (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    cutoff_at timestamptz NOT NULL,
    generation integer NOT NULL,
    run_id text NOT NULL,
    ranked integer NOT NULL CHECK (ranked >= 0),
    held integer NOT NULL CHECK (held >= 0),
    new_cards integer NOT NULL CHECK (new_cards >= 0),
    abstained integer NOT NULL CHECK (abstained >= 0),
    PRIMARY KEY (namespace_id, snapshot_id, cutoff_at)
);

-- Immutable card snapshot (ForecastCard JSON) and what the list needs to hold its place.
CREATE TABLE IF NOT EXISTS forecast_cards (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    card_id text NOT NULL,
    journal_position bigint NOT NULL CHECK (journal_position >= 1),
    object_id text,
    target_spec_id text NOT NULL,
    horizon text NOT NULL,
    status text NOT NULL CHECK (status IN ('scored', 'abstained')),
    issued_at timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    published_at timestamptz NOT NULL,
    generation integer NOT NULL,
    run_id text NOT NULL,
    -- Every candidate channel of the pair at the cutoff (the card lists the top ones).
    candidate_channel_ids jsonb NOT NULL,
    card jsonb NOT NULL,
    PRIMARY KEY (namespace_id, snapshot_id, card_id),
    UNIQUE (namespace_id, snapshot_id, journal_position),
    CHECK (window_end > issued_at),
    CHECK (published_at >= issued_at AND published_at < window_end)
);

CREATE INDEX IF NOT EXISTS forecast_cards_issue_idx
ON forecast_cards (namespace_id, snapshot_id, issued_at, card_id);

CREATE INDEX IF NOT EXISTS forecast_cards_object_idx
ON forecast_cards (namespace_id, snapshot_id, object_id, issued_at);

-- Outcome as known at the scope's data_as_of; views at an earlier as_of derive from it.
CREATE TABLE IF NOT EXISTS forecast_card_outcomes (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    card_id text NOT NULL,
    status text NOT NULL CHECK (status IN (
      'pending', 'realized', 'not_realized', 'event_without_forecast',
      'no_event_without_forecast', 'unknown'
    )),
    release_at timestamptz,
    resolved_at timestamptz,
    first_event_id text,
    first_event_at timestamptz,
    event_channel_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    event_cluster_size integer,
    unknown_reason text CHECK (unknown_reason IN (
      'source_coverage', 'policy_day', 'data_end', 'label_spec_changed'
    )),
    other_events_on_object_count integer,
    generation integer NOT NULL,
    PRIMARY KEY (namespace_id, snapshot_id, card_id)
);

CREATE INDEX IF NOT EXISTS forecast_card_outcomes_release_idx
ON forecast_card_outcomes (namespace_id, snapshot_id, release_at)
WHERE release_at IS NOT NULL;

-- Events of the card's object inside its window (journal «события окна»).
CREATE TABLE IF NOT EXISTS forecast_card_window_events (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    card_id text NOT NULL,
    event_id text NOT NULL,
    started_at timestamptz NOT NULL,
    channel_ids jsonb NOT NULL,
    cluster_size integer NOT NULL CHECK (cluster_size >= 1),
    while_open boolean NOT NULL,
    candidate_member boolean NOT NULL,
    PRIMARY KEY (namespace_id, snapshot_id, card_id, event_id)
);

-- Append-only lifecycle of cards per run: issued / released / resolved.
CREATE TABLE IF NOT EXISTS forecast_card_log (
    namespace_id text NOT NULL,
    snapshot_id text NOT NULL,
    card_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('issued', 'released', 'resolved')),
    at timestamptz NOT NULL,
    generation integer NOT NULL,
    logged_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (namespace_id, snapshot_id, card_id, kind)
);

CREATE OR REPLACE FUNCTION infra_pulse_forecast_card_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.card IS DISTINCT FROM OLD.card
     OR NEW.card_id IS DISTINCT FROM OLD.card_id
     OR NEW.issued_at IS DISTINCT FROM OLD.issued_at
     OR NEW.candidate_channel_ids IS DISTINCT FROM OLD.candidate_channel_ids THEN
    RAISE EXCEPTION 'forecast card snapshot is immutable'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'forecast_cards'::regclass AND tgname = 'forecast_cards_immutable'
  ) THEN
    CREATE TRIGGER forecast_cards_immutable
    BEFORE UPDATE ON forecast_cards
    FOR EACH ROW EXECUTE FUNCTION infra_pulse_forecast_card_immutable();
  END IF;
END $$;
