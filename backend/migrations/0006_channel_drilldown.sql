-- Keeps object/system/channel drilldown bounded to one operational scope.
CREATE INDEX IF NOT EXISTS dispatch_observations_channel_drilldown_idx
ON dispatch_observations
(namespace_id, snapshot_id, object_id, system_type, channel_id,
 available_at DESC, event_at DESC, row_uid DESC);
