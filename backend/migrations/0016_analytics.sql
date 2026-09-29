-- OPS-G (27.09.2026): read-only analytics layer for Grafana (OPS-06, SHOULD/P2).
-- Schema `analytics` holds views over dispatch_observations and the loaded
-- references (0012). The product API does not read it. Group role analytics_read
-- (NOLOGIN) gets USAGE on the schema and SELECT on its views only; it has no
-- privilege on operational tables (observations, imports, forecasts, decisions,
-- sessions, audit). The views run with their owner's rights, so the role never
-- needs the underlying tables. The Grafana login is created at deploy time from a
-- server secret (deploy/grafana/create-reader.sh); no password is kept here.
-- Idempotent: the loader and the worker apply every file on each run.
--
-- Texts stay verbatim: value_raw is never rewritten or parsed into a state code.
-- The source flag `alarm` is exposed as source_alarm and is not a confirmed
-- failure; the text «Неисправен» is technical_fault_state_proxy, not a breakdown.

CREATE SCHEMA IF NOT EXISTS analytics;
REVOKE ALL ON SCHEMA analytics FROM PUBLIC;

-- Observation scopes (historical replay snapshots and received streams): the
-- dashboards select one, so a query uses the channel index of dispatch_observations.
CREATE OR REPLACE VIEW analytics.scopes AS
SELECT namespace_id, snapshot_id, scope_kind, window_start, window_end,
       row_count, alarm_count, last_received_at
FROM dispatch_replay_snapshots;

-- Channel attributes per reference version. As in the loader, a channel listed with
-- different object, sensor type or system type is ambiguous and gets none of them
-- (its records are stored with reference_conflict); a name or tag listed with
-- different texts is left empty.
CREATE OR REPLACE VIEW analytics.channel_versions AS
SELECT reference_version, channel_id, is_ambiguous,
       CASE WHEN names = 1 THEN channel_name END AS channel_name,
       CASE WHEN NOT is_ambiguous THEN system_type END AS system_type,
       CASE WHEN NOT is_ambiguous THEN sensor_type END AS sensor_type,
       CASE WHEN tags = 1 THEN tag END AS tag,
       CASE WHEN NOT is_ambiguous THEN object_id END AS object_id
FROM (
    SELECT version_id AS reference_version, channel_id,
           count(DISTINCT (object_id, sensor_type, system_type)) > 1 AS is_ambiguous,
           count(DISTINCT name) AS names, count(DISTINCT tag) AS tags,
           min(name) AS channel_name, min(system_type) AS system_type,
           min(sensor_type) AS sensor_type, min(tag) AS tag, min(object_id) AS object_id
    FROM ref_channels
    GROUP BY version_id, channel_id
) AS grouped;

-- Dispatcher objects of the active objects reference (newest activation). Grouped by
-- object_id alone, so a join that does not use its columns is removed by the planner.
CREATE OR REPLACE VIEW analytics.objects AS
SELECT reference_version, object_id, is_ambiguous,
       CASE WHEN NOT is_ambiguous THEN object_name END AS object_name,
       CASE WHEN NOT is_ambiguous THEN object_kind END AS object_kind,
       CASE WHEN NOT is_ambiguous THEN level_raw END AS level_raw,
       CASE WHEN NOT is_ambiguous THEN parent_raw END AS parent_raw
FROM (
    SELECT min(version_id) AS reference_version, object_id,
           count(DISTINCT (level_raw, parent_raw, object_kind, name)) > 1 AS is_ambiguous,
           min(name) AS object_name, min(object_kind) AS object_kind,
           min(level_raw) AS level_raw, min(parent_raw) AS parent_raw
    FROM ref_objects
    WHERE version_id = (
        SELECT version_id FROM ref_versions
        WHERE kind = 'objects' ORDER BY activation_seq DESC LIMIT 1
    )
    GROUP BY object_id
) AS grouped;

-- Channels of the active channel reference with object names: the dashboard
-- selectors (object, sensor type, channel) read this small view, not observations.
-- Grouped by channel_id alone: a lookup by channel reads only that channel's lines.
CREATE OR REPLACE VIEW analytics.channels AS
SELECT channel.reference_version, channel.channel_id,
       CASE WHEN channel.names = 1 THEN channel.channel_name END AS channel_name,
       CASE WHEN NOT channel.is_ambiguous THEN channel.system_type END AS system_type,
       CASE WHEN NOT channel.is_ambiguous THEN channel.sensor_type END AS sensor_type,
       CASE WHEN channel.tags = 1 THEN channel.tag END AS tag,
       CASE WHEN NOT channel.is_ambiguous THEN channel.object_id END AS object_id,
       CASE WHEN NOT channel.is_ambiguous THEN object.object_name END AS object_name,
       channel.is_ambiguous
FROM (
    SELECT min(version_id) AS reference_version, channel_id,
           count(DISTINCT (object_id, sensor_type, system_type)) > 1 AS is_ambiguous,
           count(DISTINCT name) AS names, count(DISTINCT tag) AS tags,
           min(name) AS channel_name, min(system_type) AS system_type,
           min(sensor_type) AS sensor_type, min(tag) AS tag, min(object_id) AS object_id
    FROM ref_channels
    WHERE version_id = (
        SELECT version_id FROM ref_versions
        WHERE kind = 'channels' ORDER BY activation_seq DESC LIMIT 1
    )
    GROUP BY channel_id
) AS channel
LEFT JOIN analytics.objects AS object ON object.object_id = channel.object_id;

-- States of the active state reference. state_order is the order of the first line
-- of the text within its sensor type in the reference file; reference_alarm is NULL
-- when the reference lists the same text with both alarm values (alarm_conflict).
-- The reference has no channel key, so a match by type and text is an upper bound,
-- not a confirmed mapping (docs/DATA.md).
CREATE OR REPLACE VIEW analytics.states AS
SELECT version_id AS reference_version, sensor_type, state_name,
       (row_number() OVER (PARTITION BY sensor_type ORDER BY min(line_no)))::integer AS state_order,
       CASE WHEN count(DISTINCT alarm) = 1 THEN bool_and(alarm) END AS reference_alarm,
       count(DISTINCT alarm) > 1 AS alarm_conflict,
       string_agg(DISTINCT state_set_id, ', ' ORDER BY state_set_id) AS state_set_ids
FROM ref_states
WHERE version_id = (
    SELECT version_id FROM ref_versions
    WHERE kind = 'states' ORDER BY activation_seq DESC LIMIT 1
)
GROUP BY version_id, sensor_type, state_name;

-- Every observation of every scope. Names come from the reference version the
-- record was loaded with, else from the active reference (replay rows carry a
-- curated reference version that is not in ref_channels). Object, sensor and
-- system type are the values stored at load time. The name joins are unique on
-- their keys, so a query that does not read names (the dashboard selectors) scans
-- only dispatch_observations by its channel index.
CREATE OR REPLACE VIEW analytics.signals AS
SELECT observation.namespace_id, observation.snapshot_id,
       observation.event_at, observation.available_at,
       observation.object_id, object.object_name,
       observation.system_type, observation.sensor_type,
       observation.channel_id,
       coalesce(
         loaded.channel_name,
         (SELECT current_channel.channel_name FROM analytics.channels AS current_channel
          WHERE current_channel.channel_id = observation.channel_id)
       ) AS channel_name,
       observation.value_raw, observation.value_numeric,
       observation.alarm AS source_alarm,
       observation.reference_version, observation.quality_flags,
       observation.quality_flags ? 'is_epoch_placeholder' AS is_epoch_placeholder
FROM dispatch_observations AS observation
LEFT JOIN analytics.channel_versions AS loaded
  ON loaded.reference_version = observation.reference_version
 AND loaded.channel_id = observation.channel_id
LEFT JOIN analytics.objects AS object ON object.object_id = observation.object_id;

-- Numeric values (gas, temperature and other plain decimals): time series.
CREATE OR REPLACE VIEW analytics.signal_numeric AS
SELECT namespace_id, snapshot_id, event_at, object_id, object_name,
       system_type, sensor_type, channel_id, channel_name,
       value_numeric, value_raw, source_alarm, quality_flags
FROM analytics.signals
WHERE value_numeric IS NOT NULL AND NOT is_epoch_placeholder;

-- Text values verbatim with the order of the text in the state reference (NULL when
-- the text of this sensor type is not in the reference): state timeline.
CREATE OR REPLACE VIEW analytics.signal_state AS
SELECT signal.namespace_id, signal.snapshot_id, signal.event_at,
       signal.object_id, signal.object_name, signal.system_type, signal.sensor_type,
       signal.channel_id, signal.channel_name, signal.value_raw,
       state.state_order, state.reference_alarm,
       state.state_name IS NOT NULL AS in_state_reference,
       signal.source_alarm, signal.is_epoch_placeholder, signal.quality_flags
FROM analytics.signals AS signal
LEFT JOIN analytics.states AS state
  ON state.sensor_type = signal.sensor_type
 AND state.state_name = signal.value_raw
WHERE signal.value_numeric IS NULL;

-- Records with the source flag alarm = true: annotations. Not confirmed failures.
CREATE OR REPLACE VIEW analytics.source_alarms AS
SELECT namespace_id, snapshot_id, event_at, object_id, object_name,
       system_type, sensor_type, channel_id, channel_name,
       value_raw, value_numeric, quality_flags
FROM analytics.signals
WHERE source_alarm;

-- Group role of the analytics readers. Creating it needs CREATEROLE (the stand's
-- migration user is the database superuser of the postgres image); without it the
-- views are still created and the role is left to a DBA with a warning, so the
-- ingestion worker, which applies migrations at start, keeps working.
DO $$
DECLARE
  can_manage boolean;
BEGIN
  SELECT rolsuper OR rolcreaterole INTO can_manage FROM pg_roles WHERE rolname = current_user;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_read') THEN
    IF NOT can_manage THEN
      RAISE WARNING 'role analytics_read is missing and % cannot create roles: '
        'create it as a DBA (deploy/README.md, Grafana)', current_user;
      RETURN;
    END IF;
    BEGIN
      CREATE ROLE analytics_read NOLOGIN;
    EXCEPTION WHEN duplicate_object THEN
      NULL;  -- created by a concurrent migration run
    END;
  END IF;
  IF can_manage THEN
    -- Kept on the group as the reference values; a group's settings are not
    -- inherited, so create-reader.sh sets the same values on the login itself.
    ALTER ROLE analytics_read NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOREPLICATION NOBYPASSRLS;
    ALTER ROLE analytics_read SET default_transaction_read_only = on;
    ALTER ROLE analytics_read SET statement_timeout = '10s';
  END IF;
  GRANT USAGE ON SCHEMA analytics TO analytics_read;
  GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO analytics_read;
END $$;
