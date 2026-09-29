-- OPS-G (28.09.2026): Grafana dashboards on the stand data (OPS-06, SHOULD/P2).
--
-- 1. Directory fallback. The stand history is seeded without the B1 references
--    (backend/scripts/seed_forecast_history.py fills only the forecast channel layout
--    and the object names of migration 0013), so the 0016 views had no objects,
--    channels or names and every Grafana selector stayed empty. analytics.objects and
--    analytics.channels now take the active reference first and add, for what it does
--    not list, the forecast layout (forecast_objects, forecast_channel_layout). The
--    worker rebuilds that layout from every new active channel reference, so after a
--    reference upload both sources agree. A channel of the active reference keeps the
--    0016 rules: listed with different object, sensor or system type it is ambiguous
--    and gets none of them; a name or tag listed with different texts stays empty.
--    analytics.signals (0016) takes its current channel name from analytics.channels
--    and therefore gets the layout names too. The row estimate of these grouped views
--    is a default, so the dashboards attach names after limiting the records rather
--    than joining them to every record.
-- 2. Indexes for the dashboards: event time within a scope (Grafana $__timeFilter with
--    the latest records first, first and last record of a scope) and numeric records
--    only (the numeric dashboard and its selectors do not read the text records of the
--    same channels). Built once; CREATE INDEX blocks writes to dispatch_observations
--    while it runs (seconds on the 3.4 M records of the stand).
--
-- Every file is applied on each run, in order and in one transaction: 0016 restores
-- its definitions of these two views and this file replaces them again. Column
-- lists and types are those of 0016 (CREATE OR REPLACE VIEW cannot drop or change a
-- column), and the grants of 0016 stay on the replaced views. Texts stay verbatim;
-- source_alarm is the source flag, not a confirmed failure.

CREATE INDEX IF NOT EXISTS dispatch_observations_event_time_idx
ON dispatch_observations (namespace_id, snapshot_id, event_at);

CREATE INDEX IF NOT EXISTS dispatch_observations_numeric_idx
ON dispatch_observations (namespace_id, snapshot_id, channel_id, event_at)
WHERE value_numeric IS NOT NULL;

-- Objects: the active objects reference, then the forecast object names for objects it
-- does not list. Grouped by object_id, so the join in analytics.signals stays removable.
CREATE OR REPLACE VIEW analytics.objects AS
SELECT min(directory.reference_version) AS reference_version, directory.object_id,
       bool_or(directory.is_ambiguous) AS is_ambiguous,
       min(directory.object_name) AS object_name, min(directory.object_kind) AS object_kind,
       min(directory.level_raw) AS level_raw, min(directory.parent_raw) AS parent_raw
FROM (
    SELECT grouped.reference_version, grouped.object_id, grouped.is_ambiguous,
           CASE WHEN NOT grouped.is_ambiguous THEN grouped.object_name END AS object_name,
           CASE WHEN NOT grouped.is_ambiguous THEN grouped.object_kind END AS object_kind,
           CASE WHEN NOT grouped.is_ambiguous THEN grouped.level_raw END AS level_raw,
           CASE WHEN NOT grouped.is_ambiguous THEN grouped.parent_raw END AS parent_raw
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
    ) AS grouped
    UNION ALL
    SELECT forecast.reference_version, forecast.object_id, false,
           forecast.object_name, NULL, NULL, NULL
    FROM forecast_objects AS forecast
    WHERE NOT EXISTS (
        SELECT 1 FROM ref_objects AS reference
        WHERE reference.object_id = forecast.object_id
          AND reference.version_id = (
              SELECT version_id FROM ref_versions
              WHERE kind = 'objects' ORDER BY activation_seq DESC LIMIT 1
          )
    )
) AS directory
GROUP BY directory.object_id;

-- Channels: the active channel reference, then the forecast channel layout for channels
-- it does not list; object names from analytics.objects. Grouped by channel_id.
CREATE OR REPLACE VIEW analytics.channels AS
SELECT min(directory.reference_version) AS reference_version, directory.channel_id,
       min(directory.channel_name) AS channel_name,
       min(directory.system_type) AS system_type,
       min(directory.sensor_type) AS sensor_type,
       min(directory.tag) AS tag,
       min(directory.object_id) AS object_id,
       min(object.object_name) AS object_name,
       bool_or(directory.is_ambiguous) AS is_ambiguous
FROM (
    SELECT grouped.reference_version, grouped.channel_id,
           CASE WHEN grouped.names = 1 THEN grouped.channel_name END AS channel_name,
           CASE WHEN NOT grouped.is_ambiguous THEN grouped.system_type END AS system_type,
           CASE WHEN NOT grouped.is_ambiguous THEN grouped.sensor_type END AS sensor_type,
           CASE WHEN grouped.tags = 1 THEN grouped.tag END AS tag,
           CASE WHEN NOT grouped.is_ambiguous THEN grouped.object_id END AS object_id,
           grouped.is_ambiguous
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
    ) AS grouped
    UNION ALL
    SELECT layout.reference_version, layout.channel_id, layout.name, layout.system_type,
           layout.sensor_type, layout.tag, layout.object_id, false
    FROM forecast_channel_layout AS layout
    WHERE NOT EXISTS (
        SELECT 1 FROM ref_channels AS reference
        WHERE reference.channel_id = layout.channel_id
          AND reference.version_id = (
              SELECT version_id FROM ref_versions
              WHERE kind = 'channels' ORDER BY activation_seq DESC LIMIT 1
          )
    )
) AS directory
LEFT JOIN analytics.objects AS object ON object.object_id = directory.object_id
GROUP BY directory.channel_id;
