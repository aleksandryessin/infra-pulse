-- C0.4 (27.09.2026): decision deadlines and check results after a decision.
-- 0017 is reserved for B4, 0018 is used by G1. Idempotent: the loader and the worker
-- apply every file on each run; adding nullable columns does not rewrite the table.
--
-- «Сообщено энергетику» (R3) awaits a check result until awaiting_result_until;
-- «Под наблюдением» (R1) is watched until watch_until. Both are later than the decision.
-- The API enforces that R3 carries recipient, time and deadline and R1 its deadline
-- (contract ForecastDecisionCreate); old revisions keep NULL.

ALTER TABLE forecast_decisions
    ADD COLUMN IF NOT EXISTS awaiting_result_until timestamptz,
    ADD COLUMN IF NOT EXISTS watch_until timestamptz;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'forecast_decisions'::regclass
      AND conname = 'forecast_decisions_deadlines_check'
  ) THEN
    ALTER TABLE forecast_decisions ADD CONSTRAINT forecast_decisions_deadlines_check CHECK (
      (awaiting_result_until IS NULL OR (decision_code = 'R3' AND awaiting_result_until > decided_at))
      AND (watch_until IS NULL OR (decision_code = 'R1' AND watch_until > decided_at))
    );
  END IF;
END $$;

-- Result of the check (append-only revisions per card). «found» is a dictionary of what
-- was fixed and is not confirmed by the customer; event_cause is recorded only for a
-- card whose event was registered (checked by the API) and feeds later retraining.
CREATE TABLE IF NOT EXISTS forecast_check_results (
    check_result_id uuid PRIMARY KEY,
    forecast_id text NOT NULL CHECK (length(forecast_id) BETWEEN 1 AND 256),
    revision integer NOT NULL CHECK (revision >= 1),
    check_result text NOT NULL
      CHECK (check_result IN ('awaiting', 'fixed', 'no_violation', 'not_done')),
    found text[] NOT NULL DEFAULT ARRAY[]::text[] CHECK (
        found <@ ARRAY[
            'breaker', 'cable', 'contactor', 'comm_module', 'cabinet_power', 'other'
        ]::text[]
    ),
    found_other_text text CHECK (found_other_text IS NULL OR length(found_other_text) BETWEEN 1 AND 200),
    result_at timestamptz NOT NULL,
    comment text CHECK (comment IS NULL OR length(comment) <= 500),
    event_cause text CHECK (event_cause IS NULL OR event_cause IN (
        'planned_outage', 'protection_trip', 'external_grid', 'smvu_channel', 'unknown'
    )),
    actor_id text NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 256),
    actor_role text NOT NULL CHECK (actor_role IN ('dispatcher', 'admin')),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 128),
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    request_id text NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (forecast_id, revision),
    UNIQUE (forecast_id, idempotency_key),
    CHECK (cardinality(found) = 0 OR check_result = 'fixed'),
    CHECK (('other' = ANY(found)) = (found_other_text IS NOT NULL)),
    CHECK (result_at <= recorded_at)
);

CREATE INDEX IF NOT EXISTS forecast_check_results_latest_idx
ON forecast_check_results (forecast_id, revision DESC);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'forecast_check_results'::regclass
      AND tgname = 'forecast_check_results_append_only'
  ) THEN
    CREATE TRIGGER forecast_check_results_append_only
    BEFORE UPDATE OR DELETE ON forecast_check_results
    FOR EACH ROW EXECUTE FUNCTION infra_pulse_reject_history_change();
  END IF;
END $$;
